"""
test_registry.py - Registro de dispositivos: concurrencia y metadatos de pin.

Proyecto: Ena-creaccion
"""
import json
import os
import threading
import unittest

from tests import util

util.preparar_entorno()

from device_registry import (  # noqa: E402
    DeviceRegistry, normalizar_tipo, tipo_por_nombre,
)


class TestRegistro(unittest.TestCase):

    def setUp(self):
        self.ruta = os.path.join(os.path.dirname(__file__), "tmp_devices.json")
        self._limpiar()
        self.registro = DeviceRegistry(self.ruta)

    def tearDown(self):
        self._limpiar()

    def _limpiar(self):
        for ruta in (self.ruta, self.ruta + ".tmp"):
            if os.path.exists(ruta):
                os.remove(ruta)

    # -- nombres ---------------------------------------------------------
    def test_normalizar_clave(self):
        self.assertEqual(
            self.registro.normalizar_clave("Luz del Lavaloza!"), "luz_del_lavaloza"
        )
        self.assertEqual(self.registro.normalizar_clave("  Botón  Entrada "), "boton_entrada")

    def test_tipos_de_pin(self):
        self.assertEqual(normalizar_tipo("ADc"), "adc")
        self.assertEqual(normalizar_tipo("analogico"), "adc")
        self.assertEqual(normalizar_tipo("rele"), "digital_out")
        self.assertEqual(normalizar_tipo("boton"), "digital_in")
        self.assertEqual(normalizar_tipo(None), "digital_in")
        self.assertEqual(tipo_por_nombre("valvula"), "digital_out")
        self.assertEqual(tipo_por_nombre("actuador"), "digital_out")
        self.assertEqual(tipo_por_nombre("sensor"), "digital_in")

    # -- ciclo de vida ---------------------------------------------------
    def test_pendientes_y_alta(self):
        mac = "3C:71:BF:88:99:AA"
        self.registro.registrar_baliza(mac, {"beacon": "ena_node", "status": "nuevo"}, True)

        pendientes = self.registro.listar_pendientes()
        self.assertEqual(len(pendientes), 1)
        self.assertTrue(pendientes[0]["autenticado"])

        disp = self.registro.registrar_dispositivo(
            "Aspersor del jardín", mac, {"valvula": 15}, "Riego"
        )
        self.assertEqual(disp["alias"], "Aspersor del jardín")
        self.assertTrue(disp["autenticado"])
        self.assertEqual(self.registro.listar_pendientes(), [])

    def test_busqueda_parcial_y_por_mac(self):
        self.registro.registrar_dispositivo("Aspersor del jardín", "AA:BB:CC:DD:EE:01", {"valvula": 15})
        self.assertIsNotNone(self.registro.buscar_dispositivo("aspersor"))
        self.assertIsNotNone(self.registro.buscar_dispositivo("AA:BB:CC:DD:EE:01"))
        self.assertIsNone(self.registro.buscar_dispositivo("inexistente"))

    def test_misma_mac_conserva_reglas(self):
        mac = "AA:BB:CC:DD:EE:02"
        self.registro.registrar_dispositivo("Luz uno", mac, {"actuador": 2})
        self.registro.actualizar_reglas("luz_uno", [[1, 4, 0, 1, 2, 1, 0, 0]])

        self.registro.registrar_dispositivo("Luz dos", mac, {"actuador": 5})
        self.assertEqual(len(self.registro.listar_dispositivos()), 1)
        nuevo = self.registro.buscar_dispositivo("luz_dos")
        self.assertEqual(len(nuevo["reglas_activas"]), 1)

    def test_reconfigurar(self):
        self.registro.registrar_dispositivo("Luz", "AA:BB:CC:DD:EE:03", {"actuador": 2})
        disp = self.registro.reconfigurar_dispositivo(
            "luz", nuevo_alias="Luz de la cocina", nuevos_pines={"actuador": 12},
            nueva_descripcion="Foco principal",
        )
        self.assertEqual(disp["alias"], "Luz de la cocina")
        self.assertEqual(disp["pines"]["actuador"]["pin"], 12)
        self.assertEqual(disp["pines"]["actuador"]["tipo"], "digital_out")
        self.assertNotIn("luz", self.registro.dispositivos)

    def test_eliminar(self):
        self.registro.registrar_dispositivo("Luz", "AA:BB:CC:DD:EE:04", {"actuador": 2})
        self.assertIsNotNone(self.registro.eliminar_dispositivo("luz"))
        self.assertEqual(self.registro.listar_dispositivos(), [])

    # -- descriptores de hardware ---------------------------------------
    def test_descriptores_hw(self):
        self.registro.registrar_dispositivo("Clima", "AA:BB:CC:DD:EE:05", {
            "sensor_temp": {"pin": "A0", "tipo": "adc"},
            "sensor_ambiente": {"pin": 4, "tipo": "dht22"},
            "actuador": {"pin": "IO2", "tipo": "digital_out"},
            "pantalla": {"pin": 0, "tipo": "oled", "sda": 0, "scl": 2},
        })
        hw = self.registro.descriptores_hw("clima")
        por_tipo = {d["tipo"]: d for d in hw}
        self.assertEqual(por_tipo["adc"]["pin"], "A0")
        self.assertEqual(por_tipo["dht22"]["pin"], 4)
        self.assertEqual(por_tipo["digital_out"]["pin"], "IO2")
        self.assertEqual(por_tipo["oled"]["sda"], 0)

    def test_tipo_por_nombre_del_pin(self):
        self.registro.registrar_dispositivo("Riego", "AA:BB:CC:DD:EE:06", {"valvula": 15})
        disp = self.registro.buscar_dispositivo("riego")
        self.assertEqual(disp["pines"]["valvula"]["tipo"], "digital_out")

    # -- persistencia ----------------------------------------------------
    def test_escritura_atomica(self):
        self.registro.registrar_dispositivo("Luz", "AA:BB:CC:DD:EE:07", {"actuador": 2})
        self.assertFalse(os.path.exists(self.ruta + ".tmp"))
        with open(self.ruta, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
        self.assertEqual(len(datos), 1)

    def test_archivo_corrupto_no_rompe(self):
        with open(self.ruta, "w", encoding="utf-8") as archivo:
            archivo.write("{esto no es json")
        registro = DeviceRegistry(self.ruta)
        self.assertEqual(registro.listar_dispositivos(), [])

    def test_altas_concurrentes(self):
        # En el demonio hay UNA sola instancia compartida por el hilo serie y
        # los hilos HTTP: el lock debe impedir que se pierdan altas.
        total_hilos = 4
        por_hilo = 15
        errores = []

        def trabajador(indice):
            try:
                for numero in range(por_hilo):
                    self.registro.registrar_dispositivo(
                        "Nodo %d-%d" % (indice, numero),
                        "AA:BB:CC:DD:%02X:%02X" % (indice, numero),
                        {"actuador": 2},
                    )
            except Exception as err:  # pragma: no cover
                errores.append(err)

        hilos = [threading.Thread(target=trabajador, args=(i,)) for i in range(total_hilos)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()

        self.assertEqual(errores, [])
        final = DeviceRegistry(self.ruta)
        self.assertEqual(len(final.listar_dispositivos()), total_hilos * por_hilo)

        with open(self.ruta, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)  # el archivo debe ser JSON válido
        self.assertEqual(len(datos), total_hilos * por_hilo)


if __name__ == "__main__":
    unittest.main()
