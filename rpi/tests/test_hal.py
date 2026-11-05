"""
test_hal.py - Capa de hardware: alias de pines, ADC, DHT, PWM y OLED.

Proyecto: Ena-creaccion
"""
import sys
import types
import unittest

from tests import util

util.preparar_entorno()

import machine  # noqa: E402  (simulador)


class DHTFalso(object):
    """Sensor DHT de mentira que cuenta cuántas veces se le pidió medir."""

    def __init__(self, pin):
        self.pin = pin
        self.temperatura = 21.5
        self.humedad = 40.0
        self.medidas = 0

    def measure(self):
        self.medidas += 1

    def temperature(self):
        return self.temperatura

    def humidity(self):
        return self.humedad


class TestAliasDePines(unittest.TestCase):
    def setUp(self):
        self.hal_mod = util.modulo_nodo("hal")

    def test_alias_esp01s(self):
        resolver = self.hal_mod.resolver_pin
        self.assertEqual(resolver("IO0"), 0)
        self.assertEqual(resolver("BOTON1"), 0)
        self.assertEqual(resolver("RX"), 3)
        self.assertEqual(resolver("IO2"), 2)
        self.assertEqual(resolver("LED"), 2)
        self.assertEqual(resolver("A0"), self.hal_mod.GPIO_ADC)
        self.assertEqual(resolver(" adc "), self.hal_mod.GPIO_ADC)
        self.assertEqual(resolver(5), 5)
        self.assertEqual(resolver("7"), 7)
        self.assertIsNone(resolver("no-existe"))
        self.assertIsNone(resolver(None))

    def test_salidas_validas(self):
        valida = self.hal_mod.es_salida_valida
        self.assertTrue(valida(2))
        self.assertTrue(valida(16))
        self.assertFalse(valida(17))   # sólo ADC
        self.assertFalse(valida(6))    # conectado a la flash
        self.assertFalse(valida(-1))
        self.assertFalse(valida(None))


class TestSalidas(unittest.TestCase):
    def setUp(self):
        machine.reset()
        self.hal_mod = util.modulo_nodo("hal")
        self.hal = self.hal_mod.HardwareManager()

    def test_led_azul_arranca_apagado(self):
        self.hal.asegurar_pin_salida(2)
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 1)

    def test_logica_inversa_del_gpio2(self):
        self.hal.aplicar_salida(2, 1)
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 0)

        self.hal.aplicar_salida(2, 0)
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 1)

        self.hal.aplicar_salida(2, 2)  # conmutar
        self.assertEqual(machine.REGISTRO_PINES[2].value(), 0)

    def test_salida_normal_no_se_invierte(self):
        self.hal.aplicar_salida(5, 1)
        self.assertEqual(machine.REGISTRO_PINES[5].value(), 1)
        self.hal.aplicar_salida(5, 0)
        self.assertEqual(machine.REGISTRO_PINES[5].value(), 0)

    def test_pin_reservado_como_salida_se_rechaza(self):
        self.assertIsNone(self.hal.asegurar_pin_salida(17))
        self.assertIsNone(self.hal.asegurar_pin_salida(9))
        self.assertNotIn(17, machine.REGISTRO_PINES)

    def test_pwm_se_invierte_en_gpio2(self):
        self.hal.aplicar_pwm(2, 0.0)
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 65535)

        self.hal.aplicar_pwm(2, 1.0)
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 0)

        self.hal.aplicar_pwm(2, 5.0)  # fuera de rango, se recorta
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 0)

    def test_apagar_todo_apaga_pwm_y_digitales(self):
        self.hal.aplicar_pwm(2, 0.0)
        self.hal.aplicar_salida(5, 1)
        self.hal.apagar_todas_las_salidas()
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 65535)
        self.assertEqual(machine.REGISTRO_PINES[5].value(), 0)


class TestEntradas(unittest.TestCase):
    def setUp(self):
        machine.reset()
        self.reloj = util.RelojFalso(1000).instalar()
        self.hal_mod = util.modulo_nodo("hal")
        self.hal = self.hal_mod.HardwareManager()

    def tearDown(self):
        self.reloj.restaurar()
        sys.modules.pop("dht", None)

    def test_entrada_digital_con_pull_up(self):
        self.hal.asegurar_pin_entrada(4)
        pin = machine.REGISTRO_PINES[4]
        self.assertEqual(pin.modo, machine.Pin.IN)
        self.assertEqual(pin.pull, machine.Pin.PULL_UP)
        pin.value(1)
        self.assertEqual(self.hal.leer_sensor(4), 1)

    def test_adc_explicito(self):
        self.hal.asegurar_pin_entrada(17, es_adc=True)
        self.assertIsInstance(machine.REGISTRO_ADC[0], machine.ADC)
        machine.REGISTRO_ADC[0].valor = 812
        self.assertEqual(self.hal.leer_sensor(17), 812)

    def test_pin_reservado_como_entrada(self):
        self.assertIsNone(self.hal.asegurar_pin_entrada(6))

    def test_dht_con_cache_de_dos_segundos(self):
        modulo = types.ModuleType("dht")
        modulo.DHT22 = DHTFalso
        modulo.DHT11 = DHTFalso
        sys.modules["dht"] = modulo

        self.hal.asegurar_dht(4, es_dht22=True)
        info = self.hal.sensores_dht[4]
        self.assertEqual(info["tipo"], "dht22")

        self.assertEqual(self.hal.leer_sensor(4, "temp"), 21.5)
        self.assertEqual(info["sensor"].medidas, 1)

        self.reloj.avanzar(500)
        self.hal.leer_sensor(4, "temp")
        self.assertEqual(info["sensor"].medidas, 1)  # caché

        self.reloj.avanzar(2000)
        self.hal.leer_sensor(4, "hum")
        self.assertEqual(info["sensor"].medidas, 2)
        self.assertEqual(self.hal.leer_sensor(4, "hum"), 40.0)


class TestPantalla(unittest.TestCase):
    def setUp(self):
        machine.reset()
        self.hal_mod = util.modulo_nodo("hal")
        self.hal = self.hal_mod.HardwareManager()

    def test_oled_dibuja_las_lineas(self):
        self.assertTrue(self.hal.mostrar_en_oled(["Hola", "Mundo"]))
        textos = [t[0] for t in self.hal.display_oled.textos]
        self.assertIn("Hola", textos)
        self.assertIn("Mundo", textos)
        self.assertTrue(machine.SoftI2C.instancias)

    def test_oled_recorta_a_seis_lineas_y_16_caracteres(self):
        lineas = ["linea-%d" % i for i in range(10)]
        lineas[0] = "x" * 40
        self.assertTrue(self.hal.mostrar_en_oled(lineas))
        dibujados = self.hal.display_oled.textos[-6:]
        self.assertEqual(len(dibujados), 6)
        self.assertEqual(len(dibujados[0][0]), 16)

    def test_entrada_invalida(self):
        self.assertFalse(self.hal.mostrar_en_oled("no es una lista"))


if __name__ == "__main__":
    unittest.main()
