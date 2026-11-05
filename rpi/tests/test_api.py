"""
test_api.py - API local: token, transacciones y recorrido completo sin hardware.

Proyecto: Ena-creaccion

Levanta el demonio real en un puerto libre y comprueba:
- que sin token no se puede operar,
- que las órdenes firmadas que salen por el "puerto serie" son aceptadas por
  el manejador real del nodo y dejan las reglas aplicadas en el micro-PLC.

Es la prueba de integración de las tres capas (Pi -> gateway -> nodo) sin
necesidad de ESP32 ni ESP8266.
"""
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests import util

util.preparar_entorno()

import machine  # noqa: E402  (simulador)
import hermes_tools  # noqa: E402
from device_registry import DeviceRegistry  # noqa: E402
from ena_service import APIServer, SerialManager  # noqa: E402

TOKEN = "token-de-pruebas-0123456789"
CLAVE = b"secreto-de-pruebas-0123456789"
MAC = "A0:20:A6:11:22:33"

REGLA_1 = [1, 4, 0, 1, 2, 1, 0, 0]
REGLA_2 = [2, 3, 0, 0, 5, 1, 3, 500]


class GestorEspia(SerialManager):
    """SerialManager que en vez de escribir al puerto guarda las tramas."""

    def __init__(self, *args, **kwargs):
        SerialManager.__init__(self, *args, **kwargs)
        self.enviados = []

    def enviar(self, mac, payload):
        self.enviados.append((mac, json.loads(json.dumps(payload))))
        return True


def puerto_libre():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestAPI(unittest.TestCase):

    def setUp(self):
        self.ruta = os.path.join(os.path.dirname(__file__), "tmp_api.json")
        for sufijo in ("", ".tmp"):
            if os.path.exists(self.ruta + sufijo):
                os.remove(self.ruta + sufijo)

        self.registro = DeviceRegistry(self.ruta)
        self.serial = GestorEspia(self.registro, mock=True, clave=CLAVE)

        APIServer.registry = self.registro
        APIServer.serial_mgr = self.serial
        APIServer.token = TOKEN
        os.environ["ENA_TOKEN"] = TOKEN

        self.puerto = puerto_libre()
        self.base = "http://127.0.0.1:%d" % self.puerto
        self.servidor = ThreadingHTTPServer(("127.0.0.1", self.puerto), APIServer)
        self.servidor.daemon_threads = True
        self.hilo = threading.Thread(target=self.servidor.serve_forever, daemon=True)
        self.hilo.start()
        hermes_tools.BASE_URL = self.base

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        os.environ.pop("ENA_TOKEN", None)
        os.environ.pop("ENA_PERMITIR_SIN_FIRMA", None)
        APIServer.token = None
        APIServer.registry = None
        APIServer.serial_mgr = None
        for sufijo in ("", ".tmp"):
            if os.path.exists(self.ruta + sufijo):
                os.remove(self.ruta + sufijo)

    # -- utilidades ------------------------------------------------------
    def peticion(self, metodo, ruta, datos=None, token=TOKEN):
        cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else None
        req = urllib.request.Request(self.base + ruta, data=cuerpo, method=metodo)
        req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as respuesta:
                return respuesta.status, json.loads(respuesta.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def registrar(self, alias="Luz del taller", mac=MAC, **extra):
        datos = {
            "alias": alias,
            "mac": mac,
            "descripcion": "Luz de pruebas",
            "pines": {
                "sensor": {"pin": 4, "tipo": "digital_in"},
                "actuador": {"pin": 2, "tipo": "digital_out"},
            },
        }
        datos.update(extra)
        return self.peticion("POST", "/api/register", datos)

    def crear_nodo(self):
        machine.reset()
        hal = util.modulo_nodo("hal").HardwareManager()
        motor = util.modulo_nodo("motor_reglas").MotorReglas(hal)
        self.persistido = []
        manejador = util.modulo_nodo("manejador").ManejadorPaquetes(
            motor, MAC, clave=CLAVE,
            persistir=lambda reglas, hw, n: self.persistido.append((reglas, hw)),
        )
        return manejador, motor

    def alimentar_nodo(self, manejador):
        for _mac, cuerpo in self.serial.enviados:
            manejador.procesar(json.dumps(cuerpo).encode("utf-8"))

    # -- autenticación ---------------------------------------------------
    def test_ping_no_requiere_token(self):
        codigo, datos = self.peticion("GET", "/api/ping", token=None)
        self.assertEqual(codigo, 200)
        self.assertTrue(datos["pong"])

    def test_sin_token_no_se_puede_operar(self):
        codigo, datos = self.peticion("GET", "/api/devices", token=None)
        self.assertEqual(codigo, 401)
        self.assertEqual(datos["error"], "no_autorizado")

    def test_token_incorrecto(self):
        codigo, _ = self.peticion("GET", "/api/devices", token="otro")
        self.assertEqual(codigo, 401)

    def test_escritura_sin_token(self):
        codigo, _ = self.peticion(
            "POST", "/api/register",
            {"alias": "x", "mac": MAC}, token=None
        )
        self.assertEqual(codigo, 401)

    def test_status(self):
        codigo, datos = self.peticion("GET", "/api/status")
        self.assertEqual(codigo, 200)
        self.assertEqual(datos["autenticacion"], "hmac")
        self.assertTrue(datos["modo_mock"])
        self.assertEqual(datos["version"], 2)

    # -- registro --------------------------------------------------------
    def test_registrar_dispositivo(self):
        codigo, datos = self.registrar(tipo_sensor="adc")
        self.assertEqual(codigo, 200)
        disp = datos["dispositivo"]
        self.assertEqual(disp["pines"]["sensor"]["tipo"], "adc")
        self.assertEqual(disp["pines"]["actuador"]["tipo"], "digital_out")

        codigo, listado = self.peticion("GET", "/api/devices")
        self.assertEqual(codigo, 200)
        self.assertEqual(len(listado["dispositivos"]), 1)

    def test_registro_sin_firma_se_bloquea(self):
        self.registro.registrar_baliza(MAC, {"beacon": "ena_node"}, False)
        codigo, datos = self.registrar()
        self.assertEqual(codigo, 403)
        self.assertEqual(datos["error"], "nodo_sin_firma")

        os.environ["ENA_PERMITIR_SIN_FIRMA"] = "1"
        codigo, _ = self.registrar()
        self.assertEqual(codigo, 200)

    def test_registrar_exige_alias_y_mac(self):
        codigo, datos = self.peticion("POST", "/api/register", {"alias": "solo"})
        self.assertEqual(codigo, 400)

    # -- envío de reglas -------------------------------------------------
    def test_enviar_reglas_llega_al_nodo(self):
        self.registrar()
        self.serial.enviados.clear()

        codigo, datos = self.peticion("POST", "/api/send_rules", {
            "target": "luz_del_taller",
            "reglas": [REGLA_1, REGLA_2],
        })
        self.assertEqual(codigo, 200, datos)
        self.assertEqual(datos["status"], "enviado")
        self.assertGreaterEqual(len(self.serial.enviados), 3)  # begin+chunk+commit

        manejador, motor = self.crear_nodo()
        self.alimentar_nodo(manejador)
        self.assertEqual(len(motor.reglas), 2)
        self.assertEqual(self.persistido[-1][0], [REGLA_1, REGLA_2])
        self.assertEqual(self.registro.buscar_dispositivo("luz_del_taller")["reglas_activas"], [REGLA_1, REGLA_2])

    def test_enviar_reglas_firmadas(self):
        self.registrar()
        self.serial.enviados.clear()
        self.peticion("POST", "/api/send_rules", {
            "target": MAC, "reglas": [REGLA_1],
        })
        for _mac, cuerpo in self.serial.enviados:
            self.assertIn("h", cuerpo)
            self.assertIn("n", cuerpo)
            self.assertEqual(len(cuerpo["h"]), 16)

    def test_reglas_invalidas_devuelven_error(self):
        self.registrar()
        codigo, datos = self.peticion("POST", "/api/send_rules", {
            "target": "luz_del_taller", "reglas": [[1, 2, 3]],
        })
        self.assertEqual(codigo, 502)
        self.assertIn("regla_longitud", datos["error"])

    def test_borrar_reglas(self):
        self.registrar()
        self.peticion("POST", "/api/send_rules", {
            "target": "luz_del_taller", "reglas": [REGLA_1],
        })
        self.serial.enviados.clear()

        codigo, datos = self.peticion("POST", "/api/clear_rules", {
            "target": "luz_del_taller",
        })
        self.assertEqual(codigo, 200)
        self.assertEqual(datos["status"], "reglas_borradas")

        manejador, motor = self.crear_nodo()
        self.alimentar_nodo(manejador)
        self.assertEqual(motor.reglas, [])

    def test_accion_rapida_conserva_automatizaciones(self):
        self.registrar()
        self.peticion("POST", "/api/send_rules", {
            "target": "luz_del_taller", "reglas": [REGLA_1],
        })
        self.serial.enviados.clear()

        codigo, datos = self.peticion("POST", "/api/quick_action", {
            "target": "luz_del_taller", "actuador": "actuador", "segundos": 120,
        })
        self.assertEqual(codigo, 200, datos)
        self.assertEqual(datos["status"], "ejecutando")

        manejador, motor = self.crear_nodo()
        self.alimentar_nodo(manejador)
        self.assertEqual(len(motor.reglas), 2)      # la automatización sigue
        self.assertEqual(motor.reglas[-1][0], 99)   # acción temporal

    def test_accion_rapida_sobrescribe_la_anterior(self):
        self.registrar()
        for segundos in (60, 120):
            self.peticion("POST", "/api/quick_action", {
                "target": "luz_del_taller", "actuador": "actuador",
                "segundos": segundos,
            })
        reglas = self.registro.buscar_dispositivo("luz_del_taller")["reglas_activas"]
        temporales = [r for r in reglas if r[0] == 99]
        self.assertEqual(len(temporales), 1)
        self.assertEqual(temporales[0][7], 120000)

    # -- comandos directos ------------------------------------------------
    def test_identificar(self):
        self.registrar()
        self.serial.enviados.clear()
        codigo, datos = self.peticion("POST", "/api/identify", {
            "target": "luz_del_taller", "duracion_ms": 1500,
        })
        self.assertEqual(codigo, 200)
        self.assertEqual(datos["status"], "enviado")
        self.assertEqual(self.serial.enviados[-1][1]["cmd"], "identificar")
        self.assertEqual(self.serial.enviados[-1][1]["duracion_ms"], 1500)

    def test_display_se_normaliza_a_ascii(self):
        self.registrar()
        self.serial.enviados.clear()
        codigo, datos = self.peticion("POST", "/api/display", {
            "target": "luz_del_taller",
            "lineas": ["Temp: 23 °C", "Año 2026"],
        })
        self.assertEqual(codigo, 200, datos)
        self.assertEqual(datos["lineas"][0], "Temp: 23 C")
        self.assertEqual(datos["lineas"][1], "Ano 2026")

        for caracter in "".join(datos["lineas"]):
            self.assertLess(ord(caracter), 128)

    def test_display_limita_seis_lineas(self):
        self.registrar()
        codigo, datos = self.peticion("POST", "/api/display", {
            "target": "luz_del_taller",
            "lineas": ["l%d" % i for i in range(12)],
        })
        self.assertEqual(codigo, 200)
        self.assertEqual(len(datos["lineas"]), 6)

    # -- mantenimiento ----------------------------------------------------
    def test_reconfigurar(self):
        self.registrar()
        codigo, datos = self.peticion("POST", "/api/reconfigure", {
            "target": "luz_del_taller",
            "nuevo_alias": "Luz de la cocina",
            "nueva_descripcion": "Foco principal",
            "nuevos_pines": {"actuador": {"pin": 12, "tipo": "pwm"}},
        })
        self.assertEqual(codigo, 200)
        disp = datos["dispositivo"]
        self.assertEqual(disp["alias"], "Luz de la cocina")
        self.assertEqual(disp["pines"]["actuador"]["pin"], 12)
        self.assertEqual(disp["pines"]["actuador"]["tipo"], "pwm")

    def test_desvincular(self):
        self.registrar()
        codigo, datos = self.peticion("POST", "/api/delete_device", {
            "target": "luz_del_taller",
        })
        self.assertEqual(codigo, 200)
        self.assertEqual(datos["status"], "desvinculado")
        self.assertEqual(self.registro.listar_dispositivos(), [])

    def test_ruta_desconocida(self):
        codigo, _ = self.peticion("POST", "/api/no_existe", {})
        self.assertEqual(codigo, 404)

    # -- herramientas de Hermes -------------------------------------------
    def test_herramientas_hermes(self):
        self.registrar()
        texto = hermes_tools.consultar_dispositivos()
        self.assertIn("Luz del taller", texto)

        self.registro.registrar_baliza("24:6F:28:AB:12:34",
                                       {"beacon": "ena_node", "status": "nuevo"}, True)
        pendientes = hermes_tools.listar_dispositivos_nuevos()
        self.assertIn("24:6F:28:AB:12:34", pendientes)
        self.assertIn("firma verificada", pendientes)

    def test_herramientas_hermes_con_demonio_apagado(self):
        hermes_tools.BASE_URL = "http://127.0.0.1:1"
        try:
            texto = hermes_tools.consultar_dispositivos()
        finally:
            hermes_tools.BASE_URL = self.base
        self.assertIn("No se pudo contactar", texto)


if __name__ == "__main__":
    unittest.main()
