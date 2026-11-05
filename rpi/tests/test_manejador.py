"""
test_manejador.py - Lógica del nodo: autenticación, transacciones y rechazos.

Proyecto: Ena-creaccion

Ejecuta el manejador real del ESP8266 (receptor/manejador.py) sobre el motor
de reglas real y un HAL simulado. Cubre el riesgo de que un paquete basura
borrara la configuración y el de que cualquiera en el canal 1 pudiera inyectar
órdenes.
"""
import json
import unittest

from tests import util

util.preparar_entorno()

import machine  # noqa: E402  (simulador)
import protocolo as host  # noqa: E402

MAC = "A0:20:A6:11:22:33"
CLAVE = b"secreto-de-pruebas-0123456789"

REGLA_SIMPLE = [1, 4, 0, 1, 2, 1, 0, 0]
HW = [{"pin": 2, "tipo": "digital_out"}, {"pin": 4, "tipo": "digital_in"}]


class TestManejador(unittest.TestCase):

    def setUp(self):
        machine.reset()
        self.reloj = util.RelojFalso(10000).instalar()
        self.hal_mod = util.modulo_nodo("hal")
        self.motor_mod = util.modulo_nodo("motor_reglas")
        self.manejador_mod = util.modulo_nodo("manejador")

    def tearDown(self):
        self.reloj.restaurar()

    # -- utilidades -----------------------------------------------------
    def crear(self, clave=None):
        self.hal = self.hal_mod.HardwareManager()
        self.motor = self.motor_mod.MotorReglas(self.hal)
        self.persistidas = []
        self.manejador = self.manejador_mod.ManejadorPaquetes(
            self.motor,
            MAC,
            clave=clave,
            persistir=lambda reglas, hw, n: self.persistidas.append((reglas, hw, n)),
        )
        return self.manejador

    def enviar(self, paquete):
        return self.manejador.procesar(json.dumps(paquete).encode("utf-8"))

    @staticmethod
    def respuesta(respuestas):
        assert len(respuestas) == 1, respuestas
        return json.loads(respuestas[0])

    def transaccion(self, reglas, hw=None, clave=None, tx_id=7):
        cuerpos, info = host.planificar(
            reglas, mac=MAC, hw=hw, clave=clave, tx_id=tx_id
        )
        contador = 1000
        salida = []
        for cuerpo in cuerpos:
            marco = cuerpo
            if clave:
                marco = host.firmar(clave, MAC, contador, cuerpo)
            contador += 1
            salida.append(self.respuesta(
                self.manejador.procesar(json.dumps(marco).encode("utf-8"))
            ))
        return salida, info

    # -- configuración válida -------------------------------------------
    def test_reemplazo_valido(self):
        self.crear()
        resp = self.respuesta(self.enviar({"r": [REGLA_SIMPLE], "hw": HW}))
        self.assertEqual(resp["st"], "ok")
        self.assertEqual(resp["reglas"], 1)
        self.assertEqual(len(self.motor.reglas), 1)
        self.assertEqual(len(self.persistidas), 1)
        self.assertEqual(self.persistidas[0][1], HW)

    def test_reemplazo_vacio_apaga_salidas(self):
        self.crear()
        self.enviar({"r": [REGLA_SIMPLE], "hw": HW})
        self.motor.ciclo_evaluacion()
        resp = self.respuesta(self.enviar({"r": []}))
        self.assertEqual(resp["st"], "ok")
        self.assertEqual(self.motor.reglas, [])
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 1)  # apagado (Active-LOW)

    # -- rechazos que antes destruían la configuración -------------------
    def test_basura_no_borra_reglas(self):
        self.crear()
        self.enviar({"r": [REGLA_SIMPLE], "hw": HW})
        persistidas_antes = len(self.persistidas)

        for crudo in (b"esto no es json", b"[1,2,3]", b'"texto"', b"null"):
            respuestas = self.manejador.procesar(crudo)
            self.assertEqual(len(respuestas), 1)
            self.assertEqual(json.loads(respuestas[0])["st"], "error")

        self.assertEqual(len(self.motor.reglas), 1)
        self.assertEqual(len(self.persistidas), persistidas_antes)

    def test_comando_desconocido_no_borra_reglas(self):
        self.crear()
        self.enviar({"r": [REGLA_SIMPLE], "hw": HW})

        resp = self.respuesta(self.enviar({"cmd": "volcar_todo", "x": 1}))
        self.assertEqual(resp["st"], "error")
        self.assertEqual(resp["detalle"], "comando_desconocido")
        self.assertEqual(len(self.motor.reglas), 1)
        self.assertEqual(len(self.persistidas), 1)

    def test_regla_invalida_no_aplica(self):
        self.crear()
        resp = self.respuesta(self.enviar({"r": [[1, 2, 3]]}))
        self.assertEqual(resp["st"], "error")
        self.assertIn("regla_longitud", resp["detalle"])
        self.assertEqual(self.motor.reglas, [])
        self.assertEqual(self.persistidas, [])

    def test_operador_invalido_no_aplica(self):
        self.crear()
        resp = self.respuesta(self.enviar({"r": [[1, 4, 9, 1, 2, 1, 0, 0]]}))
        self.assertEqual(resp["st"], "error")
        self.assertEqual(self.motor.reglas, [])

    # -- autenticación ---------------------------------------------------
    def test_sin_firma_es_rechazado(self):
        self.crear(clave=CLAVE)
        resp = self.respuesta(self.enviar({"r": [REGLA_SIMPLE], "hw": HW}))
        self.assertEqual(resp["st"], "error")
        self.assertEqual(resp["detalle"], "auth_fail")
        self.assertEqual(resp["motivo"], "sin_firma")
        self.assertEqual(self.motor.reglas, [])
        self.assertEqual(self.manejador.errores_auth, 1)

    def test_firma_de_otro_mac_es_rechazada(self):
        self.crear(clave=CLAVE)
        marco = host.firmar(CLAVE, "AA:BB:CC:DD:EE:FF", 1, {"r": []})
        resp = self.respuesta(self.manejador.procesar(json.dumps(marco).encode()))
        self.assertEqual(resp["motivo"], "firma_invalida")

    def test_replay_es_rechazado(self):
        self.crear(clave=CLAVE)
        marco = host.firmar(CLAVE, MAC, 500, {"cmd": "ping"})
        primera = self.respuesta(self.manejador.procesar(json.dumps(marco).encode()))
        segunda = self.respuesta(self.manejador.procesar(json.dumps(marco).encode()))
        self.assertEqual(primera["st"], "ok")
        self.assertEqual(segunda["motivo"], "replay")

    def test_transaccion_firmada_completa(self):
        self.crear(clave=CLAVE)
        reglas = [[i, 4, 0, 1, 2, 1, 0, 0] for i in range(1, 6)]
        respuestas, info = self.transaccion(reglas, hw=HW, clave=CLAVE)
        self.assertEqual(respuestas[-1]["st"], "ok")
        self.assertEqual(respuestas[-1]["reglas"], len(reglas))
        self.assertEqual(len(self.motor.reglas), len(reglas))
        self.assertEqual(self.persistidas[-1][0], reglas)
        self.assertEqual(self.persistidas[-1][2], self.manejador.ultimo_contador)

    # -- transacciones ---------------------------------------------------
    def test_transaccion_incompleta_no_aplica(self):
        self.crear()
        self.enviar({"r": [REGLA_SIMPLE], "hw": HW})

        self.enviar({"cmd": "tx_begin", "id": 3, "t": 2})
        self.enviar({"cmd": "tx_chunk", "id": 3, "k": 0, "r": [[2, 4, 0, 1, 2, 1, 0, 0]]})
        resp = self.respuesta(self.enviar({"cmd": "tx_commit", "id": 3}))

        self.assertEqual(resp["st"], "error")
        self.assertIn("lotes_faltantes", resp["detalle"])
        self.assertEqual(len(self.motor.reglas), 1)  # sigue la configuración previa
        self.assertEqual(len(self.persistidas), 1)

    def test_transaccion_abortada(self):
        self.crear()
        self.enviar({"cmd": "tx_begin", "id": 4, "t": 1})
        self.enviar({"cmd": "tx_abort", "id": 4})
        resp = self.respuesta(self.enviar({"cmd": "tx_commit", "id": 4}))
        self.assertEqual(resp["st"], "error")
        self.assertEqual(self.motor.reglas, [])

    def test_transaccion_expirada_se_descarta(self):
        self.crear()
        self.enviar({"cmd": "tx_begin", "id": 5, "t": 1})
        self.reloj.avanzar(util.modulo_nodo("protocolo").VIGENCIA_TX_MS + 1)
        resp = self.respuesta(self.enviar({"cmd": "tx_commit", "id": 5}))
        self.assertEqual(resp["st"], "error")
        self.assertIn("sin_transaccion", resp["detalle"])

    def test_demasiadas_reglas(self):
        self.crear()
        reglas = [[i, 4, 0, 1, 2, 1, 0, 0] for i in range(1, 80)]
        resp = self.respuesta(self.enviar({"cmd": "tx_begin", "id": 6, "t": 1}))
        self.assertEqual(resp["st"], "ok")
        resp = self.respuesta(
            self.enviar({"cmd": "tx_chunk", "id": 6, "k": 0, "r": reglas})
        )
        self.assertEqual(resp["st"], "error")
        self.assertIn("demasiadas_reglas", resp["detalle"])

    # -- comandos directos ----------------------------------------------
    def test_identificar_parpadea_y_se_apaga(self):
        self.crear()
        resp = self.respuesta(self.enviar({"cmd": "identificar", "duracion_ms": 1000}))
        self.assertEqual(resp["st"], "ok")
        self.assertEqual(self.manejador.identificacion_hasta, 11000)

        self.reloj.fijar(10100)
        self.manejador.tick()
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 0)  # encendido

        self.reloj.fijar(11500)
        self.manejador.tick()
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 1)  # apagado
        self.assertEqual(self.manejador.identificacion_hasta, 0)

    def test_display_con_pantalla(self):
        self.crear()
        resp = self.respuesta(self.enviar({"cmd": "display", "lineas": ["Hola", "Mundo"]}))
        self.assertEqual(resp["st"], "ok")
        self.assertTrue(self.hal.display_oled is not None)

    def test_display_sin_pantalla(self):
        self.crear()
        self.hal.asegurar_oled = lambda *a, **k: None
        self.hal.display_oled = None
        resp = self.respuesta(self.enviar({"cmd": "display", "lineas": ["Hola"]}))
        self.assertEqual(resp["st"], "error")
        self.assertEqual(resp["detalle"], "sin_pantalla")

    def test_ping(self):
        self.crear()
        resp = self.respuesta(self.enviar({"cmd": "ping"}))
        self.assertEqual(resp["st"], "ok")
        self.assertEqual(resp["cmd"], "ping")

    # -- baliza ----------------------------------------------------------
    def test_baliza_sin_secreto(self):
        self.crear()
        datos = json.loads(self.manejador.construir_baliza())
        self.assertEqual(datos["beacon"], "ena_node")
        self.assertEqual(datos["auth"], 0)
        self.assertEqual(datos["status"], "nuevo")
        self.assertNotIn("h", datos)

    def test_baliza_firmada_y_verificable(self):
        self.crear(clave=CLAVE)
        datos = json.loads(self.manejador.construir_baliza())
        self.assertEqual(datos["auth"], 1)
        ok, motivo, _ = host.verificar(CLAVE, MAC, datos)
        self.assertTrue(ok, motivo)

    def test_baliza_refleja_las_reglas(self):
        self.crear()
        self.enviar({"r": [REGLA_SIMPLE], "hw": HW})
        datos = json.loads(self.manejador.construir_baliza())
        self.assertEqual(datos["status"], "listo")
        self.assertEqual(datos["reglas"], 1)


if __name__ == "__main__":
    unittest.main()
