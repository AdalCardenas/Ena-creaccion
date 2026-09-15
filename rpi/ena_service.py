"""
ena_service.py - Demonio central de comunicaciones y API local para Raspberry Pi.
Proyecto: Ena-creaccion (Capa Raspberry Pi)

Mantiene abierta la conexión serie con el Gateway ESP32, procesa balizas de
auto-descubrimiento en tiempo real y expone una API REST local (puerto 8765)
para que Hermes Agent interactúe sin bloquear el hardware.
"""
import os
import sys
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from device_registry import DeviceRegistry
from rule_compiler import RuleCompiler

PUERTO_HTTP = 8765
BAUD_RATE = 115200


class SerialManager:
    """Administra la conexión serie con el ESP32 Gateway de forma persistente."""

    def __init__(self, registry: DeviceRegistry, port: str = None, mock: bool = False):
        self.registry = registry
        self.port = port
        self.mock = mock
        self.serial_conn = None
        self.running = False
        self.lock = threading.Lock()
        self.respuestas_nodos = {}

        if not self.mock:
            self._conectar()

    def _detectar_puerto(self):
        """Busca puertos comunes en Linux/Raspberry Pi o Windows."""
        if self.port:
            return self.port

        puertos_candidatos = [
            "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0", "/dev/ttyACM1",
            "COM3", "COM4", "COM5"
        ]
        for p in puertos_candidatos:
            if os.path.exists(p) or (sys.platform == "win32" and p.startswith("COM")):
                return p
        return "/dev/ttyUSB0"

    def _conectar(self):
        try:
            import serial
            self.port = self._detectar_puerto()
            print(f"[Serial] Intentando conectar al Gateway en {self.port} a {BAUD_RATE}...")
            self.serial_conn = serial.Serial(self.port, BAUD_RATE, timeout=1)
            print(f"[Serial] Conectado exitosamente en {self.port}")
        except Exception as e:
            print(f"[Serial] Advertencia: No se pudo abrir puerto serie ({e}). Iniciando en modo simulado.")
            self.mock = True

    def iniciar_escucha(self):
        """Inicia el hilo de lectura continua de eventos desde el Gateway."""
        self.running = True
        hilo = threading.Thread(target=self._bucle_lectura, daemon=True)
        hilo.start()

    def _bucle_lectura(self):
        while self.running:
            if self.mock or not self.serial_conn:
                time.sleep(0.5)
                continue

            try:
                linea = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                if linea:
                    self._procesar_linea_gateway(linea)
            except Exception as e:
                print(f"[Serial] Error leyendo trama: {e}")
                time.sleep(1)

    def _procesar_linea_gateway(self, linea: str):
        try:
            evento = json.loads(linea)

            # Mensaje de arranque o estado del Gateway
            if "sistema" in evento:
                print(f"[Gateway Evento] {linea}")
                return

            # Mensajes de error del Gateway
            if "error" in evento:
                print(f"[Gateway Alerta] {linea}")
                return

            # Evento desde un nodo: {"from": "MAC", "data": {...}}
            if "from" in evento and "data" in evento:
                mac = evento["from"]
                datos = evento["data"]

                # Es una baliza de auto-descubrimiento
                if isinstance(datos, dict) and datos.get("beacon") == "ena_node":
                    self.registry.registrar_baliza(mac, datos)
                    print(f"[Auto-Descubrimiento] Baliza recibida de {mac}: {datos}")

                # Es una respuesta / confirmación de comando
                elif isinstance(datos, dict) and "status" in datos:
                    self.respuestas_nodos[mac] = {
                        "datos": datos,
                        "timestamp": time.time()
                    }
                    print(f"[Telemetría] Respuesta de {mac}: {datos}")
                else:
                    print(f"[Nodo {mac}] Datos: {datos}")

        except json.JSONDecodeError:
            # Si no es JSON, puede ser un traceback o mensaje de print de MicroPython
            print(f"[Gateway Salida] {linea}")

    def enviar(self, mac: str, payload: dict) -> bool:
        """Transmite una trama hacia un nodo a través del Gateway."""
        paquete = {
            "node": mac,
            "payload": payload
        }
        linea = json.dumps(paquete) + "\n"

        if self.mock or not self.serial_conn:
            print(f"[Serial MOCK] Enviando a {mac}: {linea.strip()}")
            return True

        with self.lock:
            try:
                self.serial_conn.write(linea.encode('utf-8'))
                self.serial_conn.flush()
                return True
            except Exception as e:
                print(f"[Serial] Error enviando a {mac}: {e}")
                return False


class APIServer(BaseHTTPRequestHandler):
    """Manejador HTTP ligero para responder a las herramientas de Hermes Agent."""

    registry: DeviceRegistry = None
    serial_mgr: SerialManager = None

    def _responder_json(self, codigo: int, datos: dict):
        self.send_response(codigo)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(datos, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        ruta = urlparse(self.path).path

        if ruta == "/api/status":
            self._responder_json(200, {
                "servicio": "ena_daemon",
                "estado": "en_linea",
                "modo_mock": self.serial_mgr.mock,
                "puerto": self.serial_mgr.port
            })

        elif ruta == "/api/pending":
            pendientes = self.registry.listar_pendientes()
            self._responder_json(200, {"nodos_pendientes": pendientes})

        elif ruta == "/api/devices":
            dispositivos = self.registry.listar_dispositivos()
            self._responder_json(200, {"dispositivos": dispositivos})

        else:
            self._responder_json(404, {"error": "Ruta no encontrada"})

    def do_POST(self):
        ruta = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        cuerpo = self.rfile.read(content_length).decode('utf-8')
        try:
            datos = json.loads(cuerpo) if cuerpo else {}
        except json.JSONDecodeError:
            self._responder_json(400, {"error": "JSON inválido"})
            return

        # 1. Registrar dispositivo
        if ruta == "/api/register":
            alias = datos.get("alias")
            mac = datos.get("mac")
            pines = datos.get("pines", {})
            desc = datos.get("descripcion", "")

            if not alias or not mac:
                self._responder_json(400, {"error": "Se requiere 'alias' y 'mac'"})
                return

            reg = self.registry.registrar_dispositivo(alias, mac, pines, desc)
            self._responder_json(200, {"status": "registrado", "dispositivo": reg})

        # 2. Identificar visualmente con LED
        elif ruta == "/api/identify":
            target = datos.get("target")
            duracion = datos.get("duracion_ms", 3000)

            disp = self.registry.buscar_dispositivo(target)
            mac = disp["mac"] if disp else target  # Puede ser alias o MAC directa

            exito = self.serial_mgr.enviar(mac, {"cmd": "identificar", "duracion_ms": duracion})
            self._responder_json(200, {"status": "enviado" if exito else "error", "mac": mac})

        # 3. Acción rápida por tiempo (ej. regar por 5 min)
        elif ruta == "/api/quick_action":
            target = datos.get("target")
            actuador_nombre = datos.get("actuador")
            segundos = float(datos.get("segundos", 60))

            disp = self.registry.buscar_dispositivo(target)
            if not disp:
                self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
                return

            # Resolver pin del actuador
            pin_salida = None
            if isinstance(actuador_nombre, int):
                pin_salida = actuador_nombre
            elif actuador_nombre in disp["pines"]:
                pin_info = disp["pines"][actuador_nombre]
                pin_salida = pin_info if isinstance(pin_info, int) else pin_info.get("pin")
            else:
                # Si no se especificó o no existe, tomar el primer pin disponible
                for p_nombre, p_val in disp["pines"].items():
                    pin_salida = p_val if isinstance(p_val, int) else p_val.get("pin")
                    break

            if pin_salida is None:
                self._responder_json(400, {"error": "No se encontró ningún pin de actuador en el dispositivo"})
                return

            regla = RuleCompiler.compilar_accion_temporizada(pin_salida, segundos)
            trama, _ = RuleCompiler.empaquetar_reglas([regla])
            exito = self.serial_mgr.enviar(disp["mac"], trama)

            self._responder_json(200, {
                "status": "ejecutando" if exito else "error",
                "dispositivo": disp["alias"],
                "pin": pin_salida,
                "duracion_segundos": segundos
            })

        # 4. Enviar automatización completa
        elif ruta == "/api/send_rules":
            target = datos.get("target")
            reglas = datos.get("reglas", [])

            disp = self.registry.buscar_dispositivo(target)
            if not disp:
                self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
                return

            try:
                trama, _ = RuleCompiler.empaquetar_reglas(reglas)
                exito = self.serial_mgr.enviar(disp["mac"], trama)
                if exito:
                    self.registry.actualizar_reglas(target, reglas)
                self._responder_json(200, {"status": "enviado" if exito else "error", "mac": disp["mac"]})
            except Exception as err:
                self._responder_json(400, {"error": str(err)})

        # 5. Borrar reglas de un dispositivo (desactivar automatizaciones)
        elif ruta == "/api/clear_rules":
            target = datos.get("target")
            disp = self.registry.buscar_dispositivo(target)
            if not disp:
                self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
                return

            exito = self.serial_mgr.enviar(disp["mac"], {"r": []})
            if exito:
                self.registry.limpiar_reglas(target)
            self._responder_json(200, {
                "status": "reglas_borradas" if exito else "error",
                "dispositivo": disp["alias"],
                "mac": disp["mac"]
            })

        # 6. Reconfigurar dispositivo (nombre, pines o descripción)
        elif ruta == "/api/reconfigure":
            target = datos.get("target")
            nuevo_alias = datos.get("nuevo_alias")
            nuevos_pines = datos.get("nuevos_pines")
            nueva_desc = datos.get("nueva_descripcion")

            disp_mod = self.registry.reconfigurar_dispositivo(
                target, nuevo_alias=nuevo_alias, nuevos_pines=nuevos_pines, nueva_descripcion=nueva_desc
            )
            if not disp_mod:
                self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
                return

            self._responder_json(200, {"status": "reconfigurado", "dispositivo": disp_mod})

        # 7. Eliminar / Desvincular dispositivo
        elif ruta == "/api/delete_device":
            target = datos.get("target")
            disp = self.registry.buscar_dispositivo(target)
            if not disp:
                self._responder_json(404, {"error": f"Dispositivo '{target}' no encontrado"})
                return

            # Limpiar reglas en el hardware para que quede en blanco
            self.serial_mgr.enviar(disp["mac"], {"r": []})
            eliminado = self.registry.eliminar_dispositivo(target)
            self._responder_json(200, {"status": "desvinculado", "dispositivo": eliminado})

        else:
            self._responder_json(404, {"error": "Ruta no encontrada"})


def iniciar_servicio(port: str = None, mock: bool = False, host: str = "127.0.0.1", http_port: int = PUERTO_HTTP):
    """Inicia el demonio serie y el servidor HTTP en background."""
    registry = DeviceRegistry()
    serial_mgr = SerialManager(registry, port=port, mock=mock)
    serial_mgr.iniciar_escucha()

    APIServer.registry = registry
    APIServer.serial_mgr = serial_mgr

    servidor = HTTPServer((host, http_port), APIServer)
    print(f"[Ena Service] Demonio activo. API escuchando en http://{host}:{http_port}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n[Ena Service] Deteniendo servicio...")
        serial_mgr.running = False
        servidor.server_close()


if __name__ == "__main__":
    puerto_serial = sys.argv[1] if len(sys.argv) > 1 else None
    modo_mock = "--mock" in sys.argv
    iniciar_servicio(port=puerto_serial, mock=modo_mock)
