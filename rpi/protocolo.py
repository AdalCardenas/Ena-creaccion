"""
protocolo.py - Lado Raspberry Pi del protocolo v2 (firma, lotes y tamaños).

Proyecto: Ena-creaccion (Capa Raspberry Pi)

`canonizar()` y `hmac_sha256()` son una copia exacta de las del nodo
(`receptor/protocolo.py`); la prueba `tests/test_protocolo.py` verifica que
ambas implementaciones producen los mismos bytes para el mismo paquete.

Qué resuelve:
1. Autenticidad: cada orden viaja con un HMAC-SHA256 truncado a 64 bits,
   calculado con el secreto compartido. Un tercero en el canal 1 no puede
   fabricar ni reutilizar una orden.
2. Frescura: contador monótono por trama; el nodo rechaza repetidos.
3. Tamaño: las reglas se reparten en lotes de ≤ 240 bytes y se confirman con
   `tx_commit`, de modo que el límite de ESP-NOW deja de limitar la cantidad
   de automatizaciones que admite un nodo.
"""
import hashlib
import time
import unicodedata

try:
    import binascii
except ImportError:  # MicroPython
    import ubinascii as binascii

VERSION = 2
MAX_TRAMA = 240          # el gateway acepta 250; dejamos margen
MAX_LOTES = 16
MAX_REGLAS = 64
LARGO_TAG = 8
_LARGO_TAG_HEX = LARGO_TAG * 2

# Contador de prueba con 11 dígitos: dimensiona las tramas de forma conservadora.
_CONTADOR_SONDEO = 99999999999

OPERADORES = (0, 1, 2, 3, 4)
ACCIONES = (0, 1, 2)
TEMPORIZADORES = (0, 1, 2, 3, 4, 5)
LOGICAS = (0, 1)


class ErrorProtocolo(Exception):
    """Trama inválida, demasiado grande o imposible de autenticar."""


# =============================================================================
# CRIPTOGRAFÍA (idéntica al nodo)
# =============================================================================

def hmac_sha256(clave, mensaje):
    bloque = 64
    if isinstance(clave, str):
        clave = clave.encode("utf-8")
    if len(clave) > bloque:
        clave = hashlib.sha256(clave).digest()
    if len(clave) < bloque:
        clave = clave + b"\x00" * (bloque - len(clave))

    ipad = bytes([b ^ 0x36 for b in clave])
    opad = bytes([b ^ 0x5C for b in clave])
    interno = hashlib.sha256(ipad + mensaje).digest()
    return hashlib.sha256(opad + interno).digest()


def _hex(datos):
    return binascii.hexlify(datos).decode()


def etiqueta(clave, mac, contador, cuerpo_canonico):
    mensaje = ("ena2|" + str(mac).upper() + "|" + str(int(contador)) + "|"
               + cuerpo_canonico).encode("utf-8")
    return _hex(hmac_sha256(clave, mensaje))[: _LARGO_TAG_HEX]


def _comparar_constante(a, b):
    if len(a) != len(b):
        return False
    diferencia = 0
    for i in range(len(a)):
        diferencia |= ord(a[i]) ^ ord(b[i])
    return diferencia == 0


# =============================================================================
# SERIALIZACIÓN CANÓNICA (idéntica al nodo)
# =============================================================================

def _escapar(texto):
    salida = []
    for ch in texto:
        punto = ord(ch)
        if ch == '"':
            salida.append('\\"')
        elif ch == "\\":
            salida.append("\\\\")
        elif punto < 0x20:
            salida.append("\\u%04x" % punto)
        elif punto > 0x7E:
            raise ErrorProtocolo("caracter_no_ascii")
        else:
            salida.append(ch)
    return "".join(salida)


def canonizar(obj):
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        return '"' + _escapar(obj) + '"'
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join([canonizar(x) for x in obj]) + "]"
    if isinstance(obj, dict):
        partes = []
        for k in sorted([k for k in obj.keys()]):
            if not isinstance(k, str):
                raise ErrorProtocolo("clave_no_textual")
            partes.append('"' + _escapar(k) + '":' + canonizar(obj[k]))
        return "{" + ",".join(partes) + "}"
    raise ErrorProtocolo("tipo_no_serializable")


def tamano(paquete):
    return len(canonizar(paquete).encode("utf-8"))


# =============================================================================
# FIRMA
# =============================================================================

def firmar(clave, mac, contador, paquete):
    if not isinstance(paquete, dict):
        raise ErrorProtocolo("paquete_no_es_objeto")
    contador = int(contador)
    cuerpo = {k: v for k, v in paquete.items() if k not in ("n", "h")}
    cuerpo["n"] = contador
    cuerpo["h"] = etiqueta(clave, mac, contador, canonizar(cuerpo))
    return cuerpo


def verificar(clave, mac, paquete, ultimo_contador=0, exigir_contador=False):
    """Verifica una trama recibida de un nodo (balizas y respuestas)."""
    if not isinstance(paquete, dict):
        return False, "no_es_objeto", ultimo_contador

    firma = paquete.get("h")
    if not isinstance(firma, str) or len(firma) != _LARGO_TAG_HEX:
        return False, "sin_firma", ultimo_contador

    try:
        contador = int(paquete.get("n", -1))
    except (TypeError, ValueError):
        return False, "contador_invalido", ultimo_contador

    if exigir_contador and contador <= int(ultimo_contador):
        return False, "replay", ultimo_contador

    cuerpo = {k: v for k, v in paquete.items() if k != "h"}
    try:
        esperado = etiqueta(clave, mac, contador, canonizar(cuerpo))
    except ErrorProtocolo as err:
        return False, str(err), ultimo_contador

    if not _comparar_constante(firma, esperado):
        return False, "firma_invalida", ultimo_contador
    return True, "ok", contador


# =============================================================================
# VALIDACIÓN (misma semántica que el nodo)
# =============================================================================

def _es_entero(valor):
    return isinstance(valor, int) and not isinstance(valor, bool)


def _pin_valido(pin):
    if isinstance(pin, str):
        return len(pin) > 0
    return _es_entero(pin)


def validar_regla(regla):
    if not isinstance(regla, (list, tuple)):
        return "regla_no_es_lista"
    if len(regla) != 8:
        return "regla_longitud"
    r_id, cond, op, val, p_out, act, t_type, t_ms = regla

    if not _es_entero(r_id):
        return "id_invalido"
    if not _es_entero(p_out):
        return "pin_salida_invalido"
    if not _es_entero(act) or act not in ACCIONES:
        return "accion_invalida"
    if not _es_entero(t_type) or t_type not in TEMPORIZADORES:
        return "timer_invalido"
    if not _es_entero(t_ms) or t_ms < 0:
        return "tiempo_invalido"
    if not _es_entero(val):
        return "umbral_invalido"

    if isinstance(cond, (list, tuple)):
        if len(cond) == 0:
            return "condiciones_vacias"
        if len(cond) > 8:
            return "demasiadas_condiciones"
        if not _es_entero(op) or op not in LOGICAS:
            return "logica_invalida"
        for sub in cond:
            if not isinstance(sub, (list, tuple)) or len(sub) != 3:
                return "subcondicion_invalida"
            sub_pin, sub_op, sub_val = sub
            if not _es_entero(sub_op) or sub_op not in OPERADORES:
                return "operador_invalido"
            if isinstance(sub_pin, (list, tuple)):
                if len(sub_pin) == 0 or not _pin_valido(sub_pin[0]):
                    return "pin_entrada_invalido"
            elif not _pin_valido(sub_pin):
                return "pin_entrada_invalido"
            if not _es_entero(sub_val):
                return "umbral_invalido"
        return None

    if not _es_entero(op) or op not in OPERADORES:
        return "operador_invalido"
    if not _pin_valido(cond):
        return "pin_entrada_invalido"
    return None


def validar_reglas(reglas):
    if not isinstance(reglas, (list, tuple)):
        return None, "reglas_no_es_lista"
    if len(reglas) > MAX_REGLAS:
        return None, "demasiadas_reglas"

    limpias = []
    for indice, regla in enumerate(reglas):
        motivo = validar_regla(regla)
        if motivo:
            return None, "%s (regla %d)" % (motivo, indice)
        limpias.append(list(regla))
    return limpias, None


def validar_hw(descriptores):
    if descriptores is None:
        return [], None
    if not isinstance(descriptores, (list, tuple)):
        return None, "hw_no_es_lista"
    limpios = [dict(item) for item in descriptores if isinstance(item, dict)]
    return limpios, None


# =============================================================================
# CONTADOR MONÓTONO
# =============================================================================

class Contador:
    """
    Contador para el campo "n".

    Se basa en el reloj (100 ticks por segundo) para seguir siendo creciente
    entre reinicios del demonio, y suma uno si dos tramas caen en el mismo
    tick. Cabe en 32 bits hasta el año 2155.
    """

    def __init__(self):
        self._ultimo = int(time.time() * 10)

    def siguiente(self):
        valor = int(time.time() * 10)
        if valor <= self._ultimo:
            valor = self._ultimo + 1
        self._ultimo = valor
        return valor


# =============================================================================
# PLANIFICACIÓN DE LOTES
# =============================================================================

def planificar(reglas, mac, hw=None, clave=None, max_bytes=MAX_TRAMA, tx_id=None):
    """
    Reparte las reglas en tramas que quepan en `max_bytes`.

    Devuelve (cuerpos, info). Los cuerpos van SIN firmar: el envío los firma
    otra vez en cada reintento, porque el nodo rechaza un contador repetido.
    """
    reglas_limpias, motivo = validar_reglas(reglas)
    if motivo:
        raise ErrorProtocolo(motivo)
    hw_limpio, motivo = validar_hw(hw)
    if motivo:
        raise ErrorProtocolo(motivo)

    if tx_id is None:
        tx_id = int(time.time() * 1000) % 65521
    if not isinstance(tx_id, int) or tx_id < 0:
        raise ErrorProtocolo("id_transaccion_invalido")

    def cabe(cuerpo):
        marco = cuerpo
        if clave:
            marco = firmar(clave, mac, _CONTADOR_SONDEO, cuerpo)
        return tamano(marco) <= max_bytes

    lotes = []
    actual = []
    for regla in reglas_limpias:
        candidato = actual + [regla]
        cuerpo = {"cmd": "tx_chunk", "id": tx_id, "k": len(lotes), "r": candidato}
        if cabe(cuerpo):
            actual = candidato
            continue
        if not actual:
            raise ErrorProtocolo("regla_muy_grande")
        lotes.append(actual)
        actual = [regla]
        cuerpo = {"cmd": "tx_chunk", "id": tx_id, "k": len(lotes), "r": actual}
        if not cabe(cuerpo):
            raise ErrorProtocolo("regla_muy_grande")

    if actual:
        lotes.append(actual)
    if len(lotes) > MAX_LOTES:
        raise ErrorProtocolo("demasiados_lotes")

    inicio = {"cmd": "tx_begin", "id": tx_id, "t": len(lotes)}
    if hw_limpio:
        inicio["hw"] = hw_limpio
    if not cabe(inicio):
        raise ErrorProtocolo("hw_muy_grande")

    cuerpos = [inicio]
    for indice, lote in enumerate(lotes):
        cuerpos.append({"cmd": "tx_chunk", "id": tx_id, "k": indice, "r": lote})
    commit = {"cmd": "tx_commit", "id": tx_id}
    if not cabe(commit):
        raise ErrorProtocolo("commit_muy_grande")
    cuerpos.append(commit)

    return cuerpos, {
        "tx": tx_id,
        "lotes": len(lotes),
        "reglas": len(reglas_limpias),
    }


# =============================================================================
# TEXTO PARA EL DISPLAY
# =============================================================================

_MAPA_ASCII = {
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u",
    "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ü": "U",
    "ñ": "n", "Ñ": "N", "¿": "?", "¡": "!", "º": "o", "ª": "a",
    "–": "-", "—": "-", "“": '"', "”": '"', "€": "EUR",
}


def a_ascii(texto):
    """
    Normaliza a ASCII imprimible.

    Es necesario porque la firma recorre el texto carácter a carácter y la
    pantalla SSD1306 sólo dibuja la fuente ASCII de 8x8.
    """
    if not isinstance(texto, str):
        texto = str(texto)
    normal = unicodedata.normalize("NFKD", texto)
    salida = []
    for ch in normal:
        if ch in _MAPA_ASCII:
            salida.append(_MAPA_ASCII[ch])
        elif 0x20 <= ord(ch) <= 0x7E:
            salida.append(ch)
    return "".join(salida)
