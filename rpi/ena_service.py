"""
ena_service.py - Demonio central de comunicaciones y API local para Raspberry Pi.

Proyecto: Ena-creaccion (Capa Raspberry Pi)

Mantiene abierta la conexión serie con el Gateway ESP32, procesa balizas de
auto-descubrimiento, verifica las firmas de los nodos y expone una API REST
local (por omisión en 127.0.0.1:8765) para que Hermes Agent interactúe sin
bloquear el hardware.

Cambios de esta versión:
- La API exige `Authorization: Bearer <token>` (token en `ena_config.json`).
- Cada orden se firma con HMAC-SHA256 y la respuesta del nodo se verifica,
  de modo que un tercero en el canal 1 no puede accionar pines ni simular
  confirmaciones.
- Las configuraciones grandes viajan por lotes confirmados (`tx_*`), así que
  el límite de 250 bytes de ESP-NOW deja de recortar automatizaciones.
- El servidor HTTP es multi-hilo y el registro de dispositivos tiene lock y
  escritura atómica.
"""
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import config
import protocolo
from device_registry import DeviceRegistry, normalizar_tipo
from rule_compiler import RuleCompiler

PUERTO_HTTP = 8765
BAUD_RATE = 115200

# Espera de la confirmación del nodo y número de reintentos por trama.
TIMEOUT_ACK = 0.9
REINTENTOS = 2

# Identificador de la regla temporal de una acción rápida.
ID_ACCION_RAPIDA = 99


class SerialManager:
    """Administra la conexión serie con el ESP32 Gateway de forma persistente."""

    def __init__(self, registry: DeviceRegistry, port: str = None, mock: bool = False,
                 clave: bytes = None):
        self.registry = registry
        self.port = port
        self.mock = mock
        self.serial_conn = None
        self.running = False
        self.lock = threading.Lock()          # escritura en el puerto
        self.lock_bandeja = threading.Lock()  # respuestas de los nodos
        self.bandeja = {}                     # mac -> [(instante, datos)]
        self.clave = clave if clave is not None else config.clave()
        self.contador = protocolo.Contador()
        self.ultimo_error = None
        self.rechazos = 0

        if not self.mock:
            self._conectar()

    # =====================================================================
    # CONEXIÓN
    # =====================================================================

    def _detectar_puerto(self):
        """Busca puertos comunes en Linux/Raspberry Pi o Windows."""
        if self.port:
            return self.port

        try:
            from serial.tools import list_ports
            for info in list_ports.comports():
                if info.device:
                    return info.device
        except Exception:
            pass

        puertos_candidatos = [
            "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0", "/dev/ttyACM1",
            "COM3", "COM4", "COM5",
        ]
        for candidato in puertos_candidatos:
            if os.path.exists(candidato):
                return candidato
        if sys.platform == "win32":
            return "COM3"
        return "/dev/ttyUSB0"

    def _conectar(self):
        try:
            import serial
            self.port = self._detectar_puerto()
            print(f"[Serial] Conectando al Gateway en {self.port} a {BAUD_RATE}...")
            self.serial_conn = serial.Serial(self.port, BAUD_RATE, timeout=1)

            # Auto-reinicio por hardware del ESP32 vía DTR/RTS
            # Evita que el chip quede atrapado en el modo ROM bootloader.
            try:
                self.serial_conn.dtr = False
                self.serial_conn.rts = True
                time.sleep(0.1)
                self.serial_conn.rts = False
                time.sleep(0.4)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.reset_output_buffer()
                self.serial_conn.write(b"\n")
                self.serial_conn.flush()
            except Exception as reset_err:
                print(f"[Serial] Aviso en auto-reinicio DTR/RTS: {reset_err}")

            print(f"[Serial] Conectado exitosamente en {self.port}")
            self.ultimo_error = None
        except Exception as err:
            self.ultimo_error = str(err)
            print(f"[Serial] No se pudo abrir el puerto ({err}). Modo simulado.")
            self.serial_conn = None
            self.mock = True

    def iniciar_escucha(self):
        """Inicia el hilo de lectura continua de eventos desde el Gateway."""
        self.running = True
        hilo = threading.Thread(target=self._bucle_lectura, daemon=True)
        hilo.start()

    def detener(self):
        self.running = False

    def _bucle_lectura(self):
        while self.running:
            if self.mock or not self.serial_conn:
                time.sleep(0.5)
                continue

            try:
                linea = self.serial_conn.readline().decode("utf-8", errors="ignore").strip()
                if linea:
                    self._procesar_linea_gateway(linea)
            except Exception as err:
                self.ultimo_error = str(err)
                print(f"[Serial] Error en conexión serie: {err}. Reconectando en 2 s...")
                try:
                    if self.serial_conn:
                        self.serial_conn.close()
                except Exception:
                    pass
                self.serial_conn = None
                time.sleep(2)
                self._conectar()

    # =====================================================================
    # TRAMAS RECIBIDAS
    # =====================================================================

    def _procesar_linea_gateway(self, linea: str):
        try:
            evento = json.loads(linea)
        except json.JSONDecodeError:
            print(f"[Gateway Salida] {linea}")
            return

        if not isinstance(evento, dict):
            print(f"[Gateway Salida] {linea}")
            return

        if "sistema" in evento:
            if evento.get("sistema") == "tx" and not evento.get("ok", True):
                print(f"[Gateway Alerta] el nodo {evento.get('node')} no confirmó radio.")
            return

        if "error" in evento:
            detalle = evento.get("error")
            if detalle == "payload_muy_grande":
                print(
                    f"[Gateway Alerta] trama de {evento.get('bytes')} bytes "
                    f"(máximo {evento.get('max')})"
                )
            else:
                print(f"[Gateway Alerta] {linea}")
            return

        mac = str(evento.get("from", "")).upper()
        datos = evento.get("data")
        if not mac or not isinstance(datos, dict):
            print(f"[Gateway Salida] {linea}")
            return

        # 1. Baliza de auto-descubrimiento.
        if datos.get("beacon") == "ena_node":
            autenticado = None
            if self.clave:
                ok, motivo, _ = protocolo.verificar(
                    self.clave, mac, datos, exigir_contador=False
                )
                autenticado = ok
                if not ok:
                    self.rechazos += 1
                    if not (motivo == "sin_firma" and config.permitir_sin_firma()):
                        print(
                            f"[Auto-Descubrimiento] Baliza de {mac} rechazada "
                            f"({motivo}). Revisa que el secreto coincida."
                        )
                        return
                    print(f"[Auto-Descubrimiento] {mac} sin firmar (modo inseguro).")
            self.registry.registrar_baliza(mac, datos, autenticado)
            print(f"[Auto-Descubrimiento] Baliza de {mac}: {datos}")
            return

        # 2. Respuesta a un comando: debe venir firmada por el nodo.
        if "st" in datos:
            if self.clave:
                ok, motivo, _ = protocolo.verificar(
                    self.clave, mac, datos, exigir_contador=False
                )
                if not ok:
                    self.rechazos += 1
                    print(f"[Telemetría] Respuesta de {mac} descartada ({motivo}).")
                    return
            self._guardar_respuesta(mac, datos)
            return

        print(f"[Nodo {mac}] Datos: {datos}")

    # =====================================================================
    # BANDEJA DE RESPUESTAS
    # =====================================================================

    def _guardar_respuesta(self, mac, datos):
        with self.lock_bandeja:
            cola = self.bandeja.setdefault(mac, [])
            cola.append((time.time(), datos))
            # Nunca crecer sin límite si algo deja de responder.
            if len(cola) > 20:
                del cola[0]

    def _limpiar_bandeja(self, mac):
        with self.lock_bandeja:
            self.bandeja.pop(mac, None)

    def _esperar(self, mac, tx_id, timeout):
        limite = time.time() + timeout
        while time.time() < limite:
            with self.lock_bandeja:
                cola = self.bandeja.get(mac, [])
                for indice, (_, datos) in enumerate(cola):
                    if datos.get("id") == tx_id:
                        del cola[indice]
                        return datos
            time.sleep(0.02)
        return None

    # =====================================================================
    # ENVÍO
    # =====================================================================

    def enviar(self, mac: str, payload: dict) -> bool:
        """
        Escribe una trama en el puerto serie con el formato `MAC|json`.

        El gateway reenvía el payload sin reescribirlo, que es justo lo que el
        nodo necesita para validar la firma.
        """
        try:
            texto = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as err:
            print(f"[Serial] Payload no serializable: {err}")
            return False

        linea = f"{str(mac).upper()}|{texto}\n"

        if self.mock or not self.serial_conn:
            print(f"[Serial MOCK] Enviando a {mac}: {texto}")
            return True

        with self.lock:
            try:
                self.serial_conn.write(linea.encode("utf-8"))
                self.serial_conn.flush()
                return True
            except Exception as err:
                self.ultimo_error = str(err)
                print(f"[Serial] Error enviando a {mac}: {err}")
                return False

    def _enviar_con_ack(self, mac, cuerpo):
        """
        Firma y envía una trama, esperando la confirmación del nodo.

        Se vuelve a firmar en cada reintento porque el nodo rechaza contadores
        repetidos (protección contra repetición de tramas capturadas).
        """
        tx_id = cuerpo.get("id")

        for _ in range(REINTENTOS + 1):
            if self.clave:
                marco = protocolo.firmar(
                    self.clave, mac, self.contador.siguiente(), cuerpo
                )
            else:
                marco = cuerpo

            try:
                if protocolo.tamano(marco) > protocolo.MAX_TRAMA:
                    return {"st": "error", "detalle": "trama_muy_grande"}
            except protocolo.ErrorProtocolo as err:
                return {"st": "error", "detalle": str(err)}

            self._limpiar_bandeja(mac)
            if not self.enviar(mac, marco):
                time.sleep(0.1)
                continue

            if self.mock or not self.serial_conn:
                return {"st": "simulado", "id": tx_id}

            respuesta = self._esperar(mac, tx_id, TIMEOUT_ACK)
            if respuesta is not None:
                if respuesta.get("st") == "error" and respuesta.get("detalle") == "auth_fail":
                    # Motivo concreto para poder distinguir un secreto distinto
                    # de un contador repetido (reloj de la Pi atrasado).
                    return {
                        "st": "error",
                        "detalle": "auth_fail_%s" % respuesta.get("motivo", "desconocido"),
                        "id": tx_id,
                    }
                return respuesta

        return None

    def enviar_reglas(self, mac: str, reglas: list, hw: list = None):
        """
        Envía una configuración completa por lotes confirmados.

        Retorna {"ok": bool, ...}. Un corte a mitad de envío no deja al nodo
        con media configuración: sólo se aplica al recibir `tx_commit`.
        """
        try:
            cuerpos, info = protocolo.planificar(
                reglas, mac=mac, hw=hw, clave=self.clave
            )
        except protocolo.ErrorProtocolo as err:
            return {"ok": False, "error": str(err), "reglas": 0, "lotes": 0}

        respuesta = None
        for indice, cuerpo in enumerate(cuerpos):
            respuesta = self._enviar_con_ack(mac, cuerpo)
            if respuesta is None:
                return {
                    "ok": False,
                    "error": "sin_respuesta_del_nodo",
                    "lote": indice,
                    "lotes": info["lotes"],
                }
            if respuesta.get("st") == "error":
                return {
                    "ok": False,
                    "error": respuesta.get("detalle", "error_del_nodo"),
                    "lote": indice,
                    "lotes": info["lotes"],
                }
            if respuesta.get("st") != "simulado":
                time.sleep(0.03)  # deja respirar a la radio entre lotes

        return {"ok": True, "info": info, "respuesta": respuesta or {}}

    def enviar_comando(self, mac: str, cuerpo: dict):
        """Comando corto (identificar, display) con confirmación."""
        return self._enviar_con_ack(mac, cuerpo)


class APIServer(BaseHTTPRequestHandler):
    """Manejador HTTP ligero para responder a las herramientas de Hermes Agent."""

    registry: DeviceRegistry = None
    serial_mgr: SerialManager = None
    token = None
    protocol_version = "HTTP/1.0"

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def log_message(self, formato, *args):
        """Silencia el log por petición; el demonio ya informa lo importante."""
        return

    def _token_esperado(self):
        if APIServer.token is None:
            APIServer.token = config.token()
        return APIServer.token

    def _autorizado(self):
        esperado = self._token_esperado()
        cabecera = self.headers.get("Authorization", "")
        if not cabecera.startswith("Bearer "):
            return False
        recibido = cabecera[7:].strip()
        return hmac.compare_digest(recibido, esperado)

    def _responder_json(self, codigo: int, datos: dict):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _no_autorizado(self):
        self._responder_json(401, {
            "error": "no_autorizado",
            "detalle": "Falta la cabecera Authorization: Bearer <token>. "
                       "El token está en rpi/ena_config.json.",
        })

    def _leer_json(self):
        try:
            largo = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            largo = 0
        cuerpo = self.rfile.read(largo).decode("utf-8") if largo > 0 else ""
        if not cuerpo:
            return {}
        datos = json.loads(cuerpo)
        if not isinstance(datos, dict):
            raise ValueError("json_no_objeto")
        return datos

    @staticmethod
    def _nuevo_id():
        return int(time.time() * 1000) % 100000

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self):
        ruta = urlparse(self.path).path

        if ruta == "/api/ping":
            self._responder_json(200, {"pong": True, "version": protocolo.VERSION})
            return

        if not self._autorizado():
            self._no_autorizado()
            return

        if ruta == "/api/status":
            self._responder_json(200, {
                "servicio": "ena_daemon",
                "estado": "en_linea",
                "version": protocolo.VERSION,
                "modo_mock": self.serial_mgr.mock,
                "puerto": self.serial_mgr.port,
                "autenticacion": "hmac" if self.serial_mgr.clave else "abierta",
                "ultimo_error": self.serial_mgr.ultimo_error,
                "tramas_rechazadas": self.serial_mgr.rechazos,
            })
            return

        if ruta == "/api/pending":
            self._responder_json(200, {
                "nodos_pendientes": self.registry.listar_pendientes()
            })
            return

        if ruta == "/api/devices":
            self._responder_json(200, {
                "dispositivos": self.registry.listar_dispositivos()
            })
            return

        self._responder_json(404, {"error": "Ruta no encontrada"})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self):
        if not self._autorizado():
            self._no_autorizado()
            return

        ruta = urlparse(self.path).path
        try:
            datos = self._leer_json()
        except (ValueError, json.JSONDecodeError):
            self._responder_json(400, {"error": "JSON inválido"})
            return

        try:
            self._despachar_post(ruta, datos)
        except Exception as err:  # nunca tumbar el demonio por una petición
            self._responder_json(500, {"error": "interno", "detalle": str(err)})

    def _despachar_post(self, ruta, datos):
        # 1. Registrar dispositivo
        if ruta == "/api/register":
            self._registrar(datos)
            return

        # 2. Identificar visualmente con LED
        if ruta == "/api/identify":
            self._identificar(datos)
            return

        # 3. Acción rápida por tiempo (ej. regar por 5 min)
        if ruta == "/api/quick_action":
            self._accion_rapida(datos)
            return

        # 4. Enviar automatización completa
        if ruta == "/api/send_rules":
            self._enviar_reglas(datos)
            return

        # 5. Borrar reglas de un dispositivo
        if ruta == "/api/clear_rules":
            self._borrar_reglas(datos)
            return

        # 6. Reconfigurar dispositivo
        if ruta == "/api/reconfigure":
            self._reconfigurar(datos)
            return

        # 7. Eliminar / desvincular dispositivo
        if ruta == "/api/delete_device":
            self._desvincular(datos)
            return

        # 8. Enviar texto a pantalla OLED
        if ruta == "/api/display":
            self._mostrar(datos)
            return

        self._responder_json(404, {"error": "Ruta no encontrada"})

    # ------------------------------------------------------------------
    # Implementación de cada ruta
    # ------------------------------------------------------------------

    def _registrar(self, datos):
        alias = datos.get("alias")
        mac = datos.get("mac")
        if not alias or not mac:
            self._responder_json(400, {"error": "Se requiere 'alias' y 'mac'"})
            return

        # Si hay secreto configurado, un nodo que no firma no debe darse de alta
        # salvo que el operador lo permita explícitamente.
        autenticado = self.registry.estado_autenticacion(mac)
        if self.serial_mgr.clave and autenticado is False \
                and not config.permitir_sin_firma():
            self._responder_json(403, {
                "error": "nodo_sin_firma",
                "detalle": "El nodo no superó la verificación HMAC. Revisa que "
                           "secreto.py coincida con el secreto de la Raspberry Pi "
                           "o define ENA_PERMITIR_SIN_FIRMA=1 para laboratorio.",
            })
            return

        pines = datos.get("pines", {}) or {}
        pines = self._aplicar_tipos(pines, datos.get("tipo_sensor"),
                                    datos.get("tipo_actuador"))

        reg = self.registry.registrar_dispositivo(
            alias, mac, pines, datos.get("descripcion", "")
        )
        self._responder_json(200, {"status": "registrado", "dispositivo": reg})

    @staticmethod
    def _aplicar_tipos(pines, tipo_sensor=None, tipo_actuador=None):
        """Marca el tipo de los pines 'sensor' y 'actuador' del registro."""
        pines = dict(pines)
        if tipo_sensor and "sensor" in pines:
            entrada = pines["sensor"]
            if isinstance(entrada, dict):
                entrada = dict(entrada)
                entrada["tipo"] = normalizar_tipo(tipo_sensor)
                pines["sensor"] = entrada
            else:
                pines["sensor"] = {"pin": entrada, "tipo": normalizar_tipo(tipo_sensor)}

        if tipo_actuador and "actuador" in pines:
            entrada = pines["actuador"]
            if isinstance(entrada, dict):
                entrada = dict(entrada)
                entrada["tipo"] = normalizar_tipo(tipo_actuador, "digital_out")
                pines["actuador"] = entrada
            else:
                pines["actuador"] = {
                    "pin": entrada,
                    "tipo": normalizar_tipo(tipo_actuador, "digital_out"),
                }
        return pines

    def _resolver_destino(self, target):
        disp = self.registry.buscar_dispositivo(target)
        if disp:
            return disp, disp["mac"]
        if target and ":" in str(target):
            return None, str(target).upper()
        return None, None

    def _identificar(self, datos):
        target = datos.get("target")
        duracion = int(datos.get("duracion_ms", 3000))
        disp, mac = self._resolver_destino(target)
        if mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        respuesta = self.serial_mgr.enviar_comando(mac, {
            "cmd": "identificar", "duracion_ms": duracion, "id": self._nuevo_id()
        })
        if respuesta is None:
            self._responder_json(504, {
                "error": "sin_respuesta_del_nodo",
                "detalle": "El nodo no confirmó la orden.",
                "mac": mac,
            })
            return

        self._responder_json(200, {
            "status": "enviado", "mac": mac,
            "dispositivo": disp["alias"] if disp else mac,
        })

    def _accion_rapida(self, datos):
        target = datos.get("target")
        actuador = datos.get("actuador")
        segundos = float(datos.get("segundos", 60))

        disp, mac = self._resolver_destino(target)
        if not disp or mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        pin_salida = self._resolver_pin(disp, actuador)
        if pin_salida is None:
            self._responder_json(400, {
                "error": "No se encontró ningún pin de actuador en el dispositivo"
            })
            return

        # Se conserva la automatización existente y sólo se reemplaza la acción
        # temporal anterior (mismo r_id): antes esta ruta borraba todas las reglas.
        reglas = [r for r in (disp.get("reglas_activas") or [])
                  if not (isinstance(r, (list, tuple)) and r and r[0] == ID_ACCION_RAPIDA)]
        reglas.append(RuleCompiler.compilar_accion_temporizada(pin_salida, segundos))

        resultado = self.serial_mgr.enviar_reglas(
            mac, reglas, self.registry.descriptores_hw(disp)
        )
        if not resultado.get("ok"):
            self._responder_json(502, {
                "error": resultado.get("error", "error_envio"),
                "dispositivo": disp["alias"],
            })
            return

        self.registry.actualizar_reglas(disp["clave"], reglas)
        self._responder_json(200, {
            "status": "ejecutando",
            "dispositivo": disp["alias"],
            "pin": pin_salida,
            "duracion_segundos": segundos,
        })

    @staticmethod
    def _resolver_pin(disp, actuador):
        """Traduce el nombre del actuador ('led', 'actuador', 'IO2') a GPIO."""
        if isinstance(actuador, int):
            return actuador
        if isinstance(actuador, str) and actuador.isdigit():
            return int(actuador)

        pines = disp.get("pines") or {}
        if actuador in pines:
            info = pines[actuador]
            return info if isinstance(info, (int, str)) else info.get("pin")

        if actuador and RuleCompiler.normalizar_pin(actuador) != actuador:
            return actuador

        for _, info in pines.items():
            return info if isinstance(info, (int, str)) else info.get("pin")
        return None

    def _enviar_reglas(self, datos):
        target = datos.get("target")
        reglas = datos.get("reglas", [])

        disp, mac = self._resolver_destino(target)
        if mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        hw = self.registry.descriptores_hw(disp) if disp else None
        resultado = self.serial_mgr.enviar_reglas(mac, reglas, hw)
        if not resultado.get("ok"):
            self._responder_json(502, {
                "error": resultado.get("error", "error_envio"),
                "mac": mac,
            })
            return

        if disp:
            self.registry.actualizar_reglas(disp["clave"], reglas)

        self._responder_json(200, {
            "status": "enviado",
            "mac": mac,
            "lotes": resultado.get("info", {}).get("lotes", 0),
            "reglas": resultado.get("info", {}).get("reglas", len(reglas)),
        })

    def _borrar_reglas(self, datos):
        target = datos.get("target")
        disp, mac = self._resolver_destino(target)
        if not disp or mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        resultado = self.serial_mgr.enviar_reglas(mac, [], self.registry.descriptores_hw(disp))
        if not resultado.get("ok"):
            self._responder_json(502, {"error": resultado.get("error", "error_envio")})
            return

        self.registry.limpiar_reglas(disp["clave"])
        self._responder_json(200, {
            "status": "reglas_borradas",
            "dispositivo": disp["alias"],
            "mac": disp["mac"],
        })

    def _reconfigurar(self, datos):
        target = datos.get("target")
        nuevos_pines = datos.get("nuevos_pines")
        if nuevos_pines is not None:
            nuevos_pines = self._aplicar_tipos(
                nuevos_pines, datos.get("tipo_sensor"), datos.get("tipo_actuador")
            )

        disp_mod = self.registry.reconfigurar_dispositivo(
            target,
            nuevo_alias=datos.get("nuevo_alias"),
            nuevos_pines=nuevos_pines,
            nueva_descripcion=datos.get("nueva_descripcion"),
        )
        if not disp_mod:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        self._responder_json(200, {"status": "reconfigurado", "dispositivo": disp_mod})

    def _desvincular(self, datos):
        target = datos.get("target")
        disp, mac = self._resolver_destino(target)
        if not disp or mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        # Dejar el microcontrolador sin reglas antes de olvidarlo.
        self.serial_mgr.enviar_reglas(mac, [], self.registry.descriptores_hw(disp))
        eliminado = self.registry.eliminar_dispositivo(disp["clave"])
        self._responder_json(200, {"status": "desvinculado", "dispositivo": eliminado})

    def _mostrar(self, datos):
        target = datos.get("target")
        lineas = datos.get("lineas", [])
        if not isinstance(lineas, (list, tuple)):
            self._responder_json(400, {"error": "'lineas' debe ser una lista"})
            return

        # La pantalla y la firma sólo admiten ASCII imprimible.
        limpias = [protocolo.a_ascii(linea)[:16] for linea in list(lineas)[:6]]

        disp, mac = self._resolver_destino(target)
        if mac is None:
            self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
            return

        respuesta = self.serial_mgr.enviar_comando(mac, {
            "cmd": "display", "lineas": limpias, "id": self._nuevo_id()
        })
        if respuesta is None:
            self._responder_json(504, {"error": "sin_respuesta_del_nodo", "mac": mac})
            return

        if respuesta.get("st") == "error":
            self._responder_json(502, {
                "error": respuesta.get("detalle", "error_display"),
                "mac": mac,
            })
            return

        self._responder_json(200, {
            "status": "enviado",
            "dispositivo": disp["alias"] if disp else mac,
            "lineas": limpias,
        })


def iniciar_servicio(port: str = None, mock: bool = False, host: str = None,
                     http_port: int = None):
    """Inicia el demonio serie y el servidor HTTP en background."""
    ajustes = config.cargar_config()
    host = host or ajustes.get("host", "127.0.0.1")
    http_port = int(http_port or ajustes.get("puerto", PUERTO_HTTP))

    registry = DeviceRegistry()
    serial_mgr = SerialManager(registry, port=port, mock=mock)
    serial_mgr.iniciar_escucha()

    APIServer.registry = registry
    APIServer.serial_mgr = serial_mgr
    APIServer.token = config.token()

    servidor = ThreadingHTTPServer((host, http_port), APIServer)
    servidor.daemon_threads = True
    print(f"[Ena Service] Demonio activo. API en http://{host}:{http_port}")
    print(f"[Ena Service] Token de API en {config.ARCHIVO_CONFIG}")
    print(f"[Ena Service] Firma de tramas: "
          f"{'HMAC-SHA256' if serial_mgr.clave else 'DESACTIVADA (sin secreto)'}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n[Ena Service] Deteniendo servicio...")
        serial_mgr.detener()
        servidor.server_close()


if __name__ == "__main__":
    puerto_serial = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    modo_mock = "--mock" in sys.argv
    iniciar_servicio(port=puerto_serial, mock=modo_mock)
