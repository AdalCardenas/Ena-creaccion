"""
test_system.py - Suite de pruebas y validación del sistema Ena + Hermes Agent.
Proyecto: Ena-creaccion (Capa Raspberry Pi)
"""
import os
import sys
import time
import json
import unittest
import threading
from http.server import HTTPServer

# Asegurar que rpi/ esté en el path
sys.path.insert(0, os.path.dirname(__file__))

from rule_compiler import RuleCompiler
from device_registry import DeviceRegistry
from ena_service import APIServer, SerialManager
import hermes_tools


class TestEnaSystem(unittest.TestCase):

    def setUp(self):
        self.test_db = os.path.join(os.path.dirname(__file__), "test_devices.json")
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        self.registry = DeviceRegistry(self.test_db)

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    # 1. Pruebas del Compilador de Reglas
    def test_rule_compiler(self):
        # Caso A: Regla condicional presencia -> luz
        regla = RuleCompiler.compilar_regla_condicional(
            p_in=4, operador="detecta", valor=1, p_out=2, accion=1, tipo_timer="inmediato"
        )
        self.assertEqual(regla, [1, 4, 0, 1, 2, 1, 0, 0])

        # Caso B: Riego por 5 minutos (300 segundos = 300,000 ms)
        regla_riego = RuleCompiler.compilar_accion_temporizada(
            p_out=15, duracion_segundos=300
        )
        self.assertEqual(regla_riego, [99, -1, 4, 0, 15, 1, 2, 300000])

        # Caso C: Verificación de límite de 250 bytes
        trama, tam = RuleCompiler.empaquetar_reglas([regla, regla_riego])
        self.assertLessEqual(tam, 250)
        self.assertIn("r", trama)
        self.assertEqual(len(trama["r"]), 2)

    # 2. Pruebas del Registro de Dispositivos y Búsqueda Semántica
    def test_device_registry(self):
        # Registrar baliza de nuevo nodo
        mac_test = "3C:71:BF:88:99:AA"
        self.registry.registrar_baliza(mac_test, {"beacon": "ena_node", "status": "nuevo"})

        pendientes = self.registry.listar_pendientes()
        self.assertEqual(len(pendientes), 1)
        self.assertEqual(pendientes[0]["mac"], mac_test)

        # Bautizar dispositivo
        disp = self.registry.registrar_dispositivo(
            alias="Aspersor del jardín",
            mac=mac_test,
            pines={"valvula": 15},
            descripcion="Riego automático de plantas"
        )
        self.assertEqual(disp["alias"], "Aspersor del jardín")

        # Una vez registrado, ya no debe aparecer en pendientes
        self.assertEqual(len(self.registry.listar_pendientes()), 0)

        # Búsqueda difusa (búsqueda parcial por nombre)
        encontrado = self.registry.buscar_dispositivo("aspersor")
        self.assertIsNotNone(encontrado)
        self.assertEqual(encontrado["mac"], mac_test)

        # Búsqueda por MAC
        encontrado_mac = self.registry.buscar_dispositivo(mac_test)
        self.assertIsNotNone(encontrado_mac)
        self.assertEqual(encontrado_mac["alias"], "Aspersor del jardín")

    # 3. Prueba de Integración con el Servidor y Herramientas de Hermes
    def test_hermes_tools_integration(self):
        test_port = 8799
        hermes_tools.BASE_URL = f"http://127.0.0.1:{test_port}"

        # Iniciar servidor de prueba
        serial_mgr = SerialManager(self.registry, mock=True)
        APIServer.registry = self.registry
        APIServer.serial_mgr = serial_mgr
        server = HTTPServer(("127.0.0.1", test_port), APIServer)

        hilo_server = threading.Thread(target=server.serve_forever, daemon=True)
        hilo_server.start()
        time.sleep(0.1)

        try:
            # Simular llegada de baliza
            mac_nueva = "24:6F:28:AB:12:34"
            self.registry.registrar_baliza(mac_nueva, {"beacon": "ena_node", "status": "nuevo"})

            # A. Hermes lista dispositivos nuevos
            resp_pendientes = hermes_tools.listar_dispositivos_nuevos()
            self.assertIn(mac_nueva, resp_pendientes)

            # B. Hermes hace parpadear el dispositivo
            resp_ident = hermes_tools.identificar_dispositivo(mac_nueva, duracion_segundos=2)
            self.assertIn("Haciendo parpadear", resp_ident)

            # C. Hermes registra el dispositivo
            resp_reg = hermes_tools.registrar_dispositivo(
                alias="Luz del lavaloza",
                mac=mac_nueva,
                funcion_dispositivo="Luz activada por presencia",
                pin_sensor=4,
                pin_actuador=2
            )
            self.assertIn("He registrado el dispositivo", resp_reg)

            # D. Hermes programa una automatización
            resp_auto = hermes_tools.crear_automatizacion(
                dispositivo="Luz del lavaloza",
                pin_sensor=4,
                condicion="detecta",
                pin_actuador=2,
                accion="encender",
                tipo_temporizador="inmediato"
            )
            self.assertIn("Automatización guardada", resp_auto)

            # E. Hermes activa salida temporal (5 minutos de riego)
            resp_temp = hermes_tools.activar_salida_temporal(
                dispositivo="Luz del lavaloza",
                actuador="actuador",
                duracion_segundos=300
            )
            self.assertIn("5.0 minutos", resp_temp)

            # F. Hermes consulta lista completa
            resp_todos = hermes_tools.consultar_dispositivos()
            self.assertIn("Luz del lavaloza", resp_todos)

            # G. Reprogramación: Cambiar automatización existente (ej. parpadeo/blink)
            resp_reprog = hermes_tools.crear_automatizacion(
                dispositivo="Luz del lavaloza",
                pin_sensor=4,
                condicion="detecta",
                pin_actuador=2,
                accion="encender",
                tipo_temporizador="parpadeo",
                tiempo_segundos=1
            )
            self.assertIn("Automatización guardada", resp_reprog)

            # H. Reconfigurar dispositivo: Cambiar nombre y función
            resp_reconf = hermes_tools.reconfigurar_dispositivo(
                dispositivo="Luz del lavaloza",
                nuevo_nombre="Luz de la cocina",
                nueva_funcion="Foco principal",
                pin_actuador=12
            )
            self.assertIn("Luz de la cocina", resp_reconf)

            # I. Borrar reglas (desactivar automatizaciones)
            resp_borrar = hermes_tools.borrar_reglas("Luz de la cocina")
            self.assertIn("eliminado todas las reglas", resp_borrar)

            # J. Desvincular / Resetear dispositivo a estado nuevo
            resp_desv = hermes_tools.desvincular_dispositivo("Luz de la cocina")
            self.assertIn("ha sido desvinculado", resp_desv)
            self.assertEqual(len(self.registry.listar_dispositivos()), 0)

        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
