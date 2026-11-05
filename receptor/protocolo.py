"""
protocolo.py - Trama v2 del nodo Ena: autenticidad, integridad y transacciones.

Proyecto: Ena-creaccion / Ena-receptor (ESP8266 / ESP-01S)

Se ejecuta tanto en MicroPython (dispositivo) como en CPython (pruebas), por lo
que sólo usa hashlib y funciones básicas del lenguaje. La copia equivalente de
la Raspberry Pi vive en `rpi/protocolo.py` y una prueba verifica que ambas
generan exactamente los mismos bytes.

Formato de un paquete firmado (JSON en el aire):

    {"cmd": "tx_chunk", "id": 7, "k": 0, "r": [[...]], "n": 1729, "h": "3f8a..."}

- "n": contador monótono. El nodo exige n > último contador aceptado, de modo
  que una trama capturada por un tercero no se puede repetir.
- "h": HMAC-SHA256 truncado a 8 bytes (64 bits) en hexadecimal, calculado
  sobre "ena2|MAC_DEL_NODO|n|CUERPO_CANÓNICO".
- El cuerpo canónico se serializa con `canonizar()`: claves ordenadas, sin
  espacios y sólo ASCII. Así ambos extremos firman los mismos bytes sin
  depender de cómo serialice cada implementación de JSON.

Sin el secreto compartido no se puede fabricar un "h" válido ni reutilizar uno
antiguo, que era el riesgo principal: cualquiera en el canal 1 podía inyectar
reglas y accionar pines.
"""
import hashlib

try:
    import binascii
except ImportError:  # MicroPython
    import ubinascii as binascii

VERSION = 2

# ESP-NOW admite 250 bytes por trama; dejamos 10 de margen para el gateway.
MAX_TRAMA = 240

# Bytes de HMAC que viajan realmente en la trama (64 bits en 16 caracteres).
LARGO_TAG = 8
_LARGO_TAG_HEX = LARGO_TAG * 2

# Límites de la memoria del ESP-01S (~35 KB libres).
MAX_REGLAS = 64
MAX_LOTES = 16
MAX_HW = 12

# Vigencia de una transacción a medias antes de descartarla.
VIGENCIA_TX_MS = 15000

OPERADORES = (0, 1, 2, 3, 4)   # ==, !=, >, <, incondicional
ACCIONES = (0, 1, 2)           # apagar, encender, conmutar
TEMPORIZADORES = (0, 1, 2, 3, 4, 5)  # inmediato, TON, pulso, blink, heartbeat, fade
LOGICAS = (0, 1)               # AND, OR


class ErrorProtocolo(Exception):
    """Fallo de validación, autenticación o secuencia de una trama."""


# =============================================================================
# CRIPTOGRAFÍA (HMAC-SHA256 sin depender del módulo hmac)
# =============================================================================

def hmac_sha256(clave, mensaje):
    """
    HMAC-SHA256 en su definición RFC 2104.

    MicroPython no incluye `hmac` en todas sus compilaciones para ESP8266, así
    que se implementa sobre `hashlib.sha256`, que sí está disponible.
    """
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
    """Etiqueta de autenticación truncada, en hexadecimal minúscula."""
    mensaje = ("ena2|" + str(mac).upper() + "|" + str(int(contador)) + "|"
               + cuerpo_canonico).encode("utf-8")
    return _hex(hmac_sha256(clave, mensaje))[: _LARGO_TAG_HEX]


def _comparar_constante(a, b):
    """Comparación en tiempo constante para no filtrar la etiqueta."""
    if len(a) != len(b):
        return False
    diferencia = 0
    for i in range(len(a)):
        diferencia |= ord(a[i]) ^ ord(b[i])
    return diferencia == 0


# =============================================================================
# SERIALIZACIÓN CANÓNICA (idéntica en el nodo y en la Raspberry Pi)
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
    """
    Serialización determinista: mismas claves en el mismo orden en ambos
    extremos. Rechaza tipos que no existen en el JSON del protocolo (floats,
    binarios) para que la firma nunca dependa de detalles de implementación.
    """
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
        claves = sorted([k for k in obj.keys()])
        partes = []
        for k in claves:
            if not isinstance(k, str):
                raise ErrorProtocolo("clave_no_textual")
            partes.append('"' + _escapar(k) + '":' + canonizar(obj[k]))
        return "{" + ",".join(partes) + "}"
    raise ErrorProtocolo("tipo_no_serializable")


def tamano(paquete):
    """Bytes que ocupará el paquete al viajar por radio/serie."""
    return len(canonizar(paquete).encode("utf-8"))


# =============================================================================
# FIRMA Y VERIFICACIÓN
# =============================================================================

def firmar(clave, mac, contador, paquete):
    """Devuelve una copia del paquete con los campos "n" y "h"."""
    if not isinstance(paquete, dict):
        raise ErrorProtocolo("paquete_no_es_objeto")
    contador = int(contador)
    cuerpo = {}
    for k, v in paquete.items():
        if k not in ("n", "h"):
            cuerpo[k] = v
    cuerpo["n"] = contador
    cuerpo["h"] = etiqueta(clave, mac, contador, canonizar(cuerpo))
    return cuerpo


def verificar(clave, mac, paquete, ultimo_contador=0, exigir_contador=True):
    """
    Comprueba autenticidad y frescura.

    Retorna (ok, motivo, contador). No lanza excepciones: el nodo necesita
    responder con un error firmado en lugar de reiniciarse.
    """
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

    cuerpo = {}
    for k, v in paquete.items():
        if k != "h":
            cuerpo[k] = v
    try:
        esperado = etiqueta(clave, mac, contador, canonizar(cuerpo))
    except ErrorProtocolo as err:
        return False, str(err), ultimo_contador

    if not _comparar_constante(firma, esperado):
        return False, "firma_invalida", ultimo_contador
    return True, "ok", contador


# =============================================================================
# VALIDACIÓN SEMÁNTICA DE REGLAS
# =============================================================================

def _es_entero(valor):
    return isinstance(valor, int) and not isinstance(valor, bool)


def _pin_valido(pin):
    if isinstance(pin, str):
        return len(pin) > 0 and pin != ""
    return _es_entero(pin)


def validar_regla(regla):
    """Devuelve None si la regla es ejecutable o el motivo del rechazo."""
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

    # Regla multi-condición: condiciones = [[pin, op, val], ...]
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

    # Regla simple: [r_id, pin, op, val, ...]
    if not _es_entero(op) or op not in OPERADORES:
        return "operador_invalido"
    if not _pin_valido(cond):
        return "pin_entrada_invalido"
    return None


def validar_reglas(reglas):
    """
    Valida una lista completa.

    Retorna (lista_limpia, None) o (None, motivo). Convierte a listas las
    tuplas para que el motor reciba siempre el mismo tipo.
    """
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
    """Valida la lista de descriptores de hardware {"pin", "tipo", ...}."""
    if descriptores is None:
        return [], None
    if not isinstance(descriptores, (list, tuple)):
        return None, "hw_no_es_lista"
    if len(descriptores) > MAX_HW:
        return None, "demasiados_descriptores_hw"

    limpios = []
    for item in descriptores:
        if not isinstance(item, dict):
            return None, "hw_item_invalido"
        if not _pin_valido(item.get("pin")):
            return None, "hw_pin_invalido"
        limpios.append(dict(item))
    return limpios, None


# =============================================================================
# TRANSACCIONES: una configuración completa en varias tramas
# =============================================================================

class GestorTransacciones:
    """
    Acumula las reglas enviadas en varios lotes y sólo las entrega cuando el
    host confirma (`tx_commit`).

    Antes, un juego de reglas que no cabía en 250 bytes era sencillamente
    imposible de enviar (y un corte a mitad dejaba al nodo con media
    configuración). Con confirmación explícita, el nodo aplica todo o nada.
    """

    def __init__(self):
        self.tx = None
        self.inicio_ms = 0
        self.ultimo_commit = None

    # -- estado ------------------------------------------------------------
    def activa(self):
        return self.tx is not None

    def expirada(self, ahora_ms):
        if self.tx is None:
            return False
        return (ahora_ms - self.inicio_ms) > VIGENCIA_TX_MS

    def descartar(self):
        self.tx = None

    # -- operaciones -------------------------------------------------------
    def iniciar(self, tx_id, total, hw=None, ahora_ms=0):
        if not _es_entero(tx_id):
            raise ErrorProtocolo("id_transaccion_invalido")
        if not _es_entero(total) or total < 0 or total > MAX_LOTES:
            raise ErrorProtocolo("lotes_fuera_de_rango")
        hw_limpio, motivo = validar_hw(hw)
        if motivo:
            raise ErrorProtocolo(motivo)
        self.tx = {"id": tx_id, "total": total, "hw": hw_limpio, "lotes": {}}
        self.inicio_ms = ahora_ms
        return {"total": total, "hw": len(hw_limpio)}

    def agregar(self, tx_id, lote, reglas):
        if self.tx is None:
            raise ErrorProtocolo("sin_transaccion")
        if self.tx["id"] != tx_id:
            raise ErrorProtocolo("transaccion_distinta")
        if not _es_entero(lote) or lote < 0 or lote >= self.tx["total"]:
            raise ErrorProtocolo("lote_fuera_de_rango")

        limpias, motivo = validar_reglas(reglas)
        if motivo:
            raise ErrorProtocolo(motivo)

        # Sobrescribir el mismo lote hace la operación idempotente ante
        # reintentos del host por una respuesta perdida.
        self.tx["lotes"][lote] = limpias
        total_reglas = sum([len(v) for v in self.tx["lotes"].values()])
        if total_reglas > MAX_REGLAS:
            raise ErrorProtocolo("demasiadas_reglas")
        return len(limpias)

    def confirmar(self, tx_id):
        if self.tx is None:
            # Reintento de un commit ya aplicado: responder como éxito.
            if self.ultimo_commit and self.ultimo_commit[0] == tx_id:
                return self.ultimo_commit[1], self.ultimo_commit[2]
            raise ErrorProtocolo("sin_transaccion")
        if self.tx["id"] != tx_id:
            raise ErrorProtocolo("transaccion_distinta")

        total = self.tx["total"]
        for lote in range(total):
            if lote not in self.tx["lotes"]:
                raise ErrorProtocolo("lotes_faltantes")

        reglas = []
        for lote in range(total):
            reglas.extend(self.tx["lotes"][lote])
        hw = self.tx["hw"]

        self.ultimo_commit = (tx_id, reglas, hw)
        self.tx = None
        return reglas, hw

    def abortar(self, tx_id):
        if self.tx is not None and self.tx["id"] == tx_id:
            self.tx = None
            return True
        return False
