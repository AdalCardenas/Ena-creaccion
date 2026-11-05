"""
test_motor_reglas.py - Temporizadores, lógica compuesta y tipos de pin.

Proyecto: Ena-creaccion

Ejecuta el motor de reglas del ESP8266 en CPython con el reloj y el hardware
simulados. Cubre los seis temporizadores, la lógica AND/OR y la corrección del
modo ADC (que antes se deducía del umbral de la regla).
"""
import unittest

from tests import util

util.preparar_entorno()

import machine  # noqa: E402  (simulador)

HW_SALIDA = [{"pin": 2, "tipo": "digital_out"}]


class TestMotorReglas(unittest.TestCase):

    def setUp(self):
        machine.reset()
        self.reloj = util.RelojFalso(10000).instalar()
        self.hal_mod = util.modulo_nodo("hal")
        self.motor_mod = util.modulo_nodo("motor_reglas")
        self.protocolo = util.modulo_nodo("protocolo")

    def tearDown(self):
        self.reloj.restaurar()

    def motor_con(self, reglas, hw=None):
        self.hal = self.hal_mod.HardwareManager()
        self.motor = self.motor_mod.MotorReglas(self.hal)
        self.motor.aplicar_reglas(reglas, hw)
        return self.motor

    def pin(self, numero):
        return machine.REGISTRO_PINES[numero]

    def entrada(self, numero, valor):
        machine.REGISTRO_PINES[numero].value(valor)

    # -- lógica combinacional -------------------------------------------
    def test_inmediato(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 0, 0]], HW_SALIDA)
        self.entrada(4, 1)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # encendido (Active-LOW)

        self.entrada(4, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # apagado

    def test_accion_apagar_invierte_la_logica(self):
        self.motor_con([[1, 4, 0, 1, 2, 0, 0, 0]], HW_SALIDA)
        self.entrada(4, 1)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # apagado

        self.entrada(4, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # encendido

    def test_condicion_and(self):
        regla = [1, [[4, 0, 1], [3, 0, 0]], 0, 0, 2, 1, 0, 0]
        self.motor_con([regla], HW_SALIDA)
        self.entrada(4, 1)
        self.entrada(3, 1)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # falta una condición

        self.entrada(3, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # ambas cumplidas

    def test_condicion_or(self):
        regla = [1, [[4, 0, 1], [3, 0, 0]], 1, 0, 2, 1, 0, 0]
        self.motor_con([regla], HW_SALIDA)
        self.entrada(4, 0)
        self.entrada(3, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # una basta

        self.entrada(3, 1)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # ninguna se cumple

    # -- temporizadores --------------------------------------------------
    def test_ton_retardo_al_encendido(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 1, 500]], HW_SALIDA)
        self.entrada(4, 1)

        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # aún no

        self.reloj.avanzar(400)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # todavía no (400 < 500)

        self.reloj.avanzar(200)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # 600 >= 500

        self.entrada(4, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # se apaga y reinicia

    def test_pulso_monoestable(self):
        self.motor_con([[1, -1, 4, 0, 2, 1, 2, 300]], HW_SALIDA)

        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)

        self.reloj.avanzar(100)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # sigue el pulso

        self.reloj.avanzar(250)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # terminó el pulso

        self.reloj.avanzar(100)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # no se rearma solo

    def test_blink(self):
        self.motor_con([[1, -1, 4, 0, 2, 1, 3, 200]], HW_SALIDA)

        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)

        self.reloj.avanzar(100)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)

        self.reloj.avanzar(150)  # 250 ms
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # encendido

        self.reloj.avanzar(250)  # 500 ms
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # apagado

    def test_heartbeat(self):
        self.motor_con([[1, -1, 4, 0, 2, 1, 4, 1000]], HW_SALIDA)
        inicio = self.reloj.ms

        self.reloj.fijar(inicio)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # destello

        self.reloj.fijar(inicio + 100)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # pausa

        self.reloj.fijar(inicio + 200)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # segundo destello

        self.reloj.fijar(inicio + 300)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # pausa larga

    def test_fade_pwm(self):
        self.motor_con([[1, -1, 4, 0, 2, 1, 5, 1000]], HW_SALIDA)
        inicio = self.reloj.ms

        self.reloj.fijar(inicio)
        self.motor.ciclo_evaluacion()
        # Brillo 0 sobre LED Active-LOW => duty físico máximo.
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 65535)

        self.reloj.fijar(inicio + 250)
        self.motor.ciclo_evaluacion()
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), int(0.75 * 65535))

        self.reloj.fijar(inicio + 500)
        self.motor.ciclo_evaluacion()
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), int(0.5 * 65535))

    def test_fade_se_apaga_sin_condicion(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 5, 1000]], HW_SALIDA)
        self.entrada(4, 1)
        self.motor.ciclo_evaluacion()
        self.reloj.avanzar(100)
        self.entrada(4, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(machine.REGISTRO_PWM[2].duty_u16(), 65535)  # brillo 0

    # -- tipos de pin ----------------------------------------------------
    def test_adc_declarado(self):
        hw = [{"pin": "A0", "tipo": "adc"}, {"pin": 2, "tipo": "digital_out"}]
        self.motor_con([[1, 17, 2, 500, 2, 1, 0, 0]], hw)

        self.assertIsInstance(machine.REGISTRO_ADC[0], machine.ADC)
        machine.REGISTRO_ADC[0].valor = 700
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)  # 700 > 500

        machine.REGISTRO_ADC[0].valor = 300
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 1)  # 300 < 500

    def test_pin_sin_declarar_es_digital_aunque_el_umbral_sea_alto(self):
        # Antes, val > 1 hacía que este pin se configurara como ADC y la regla
        # jamás leyera nada.
        self.motor_con([[1, 4, 2, 500, 2, 1, 0, 0]], HW_SALIDA)
        configurado = machine.REGISTRO_PINES[4]
        self.assertIsInstance(configurado, machine.Pin)
        self.assertEqual(configurado.modo, machine.Pin.IN)
        self.assertEqual(machine.REGISTRO_ADC, {})

    def test_salida_en_pin_reservado_no_rompe_el_nodo(self):
        self.motor_con([[1, -1, 4, 0, 17, 1, 0, 0]], [])
        self.motor.ciclo_evaluacion()  # no debe lanzar
        self.assertNotIn(17, machine.REGISTRO_PINES)

    # -- administración ---------------------------------------------------
    def test_reglas_invalidas_no_reemplazan(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 0, 0]], HW_SALIDA)
        with self.assertRaises(self.protocolo.ErrorProtocolo):
            self.motor.aplicar_reglas([[1, 2, 3]])
        self.assertEqual(len(self.motor.reglas), 1)

    def test_ids_repetidos_son_independientes(self):
        reglas = [
            [1, 4, 0, 1, 2, 1, 0, 0],
            [1, 3, 0, 0, 5, 1, 0, 0],
        ]
        self.motor_con(reglas, HW_SALIDA)
        self.entrada(4, 1)
        self.entrada(3, 0)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)   # regla 1 cumple
        self.assertEqual(self.pin(5).value(), 1)   # regla 2 cumple

    def test_apagar_todo(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 0, 0]], HW_SALIDA)
        self.entrada(4, 1)
        self.motor.ciclo_evaluacion()
        self.assertEqual(self.pin(2).value(), 0)

        self.motor.apagar_todo()
        self.assertEqual(self.pin(2).value(), 1)

    def test_ciclo_sobrevive_a_una_regla_defectuosa(self):
        self.motor_con([[1, 4, 0, 1, 2, 1, 0, 0]], HW_SALIDA)
        self.motor.reglas.append(["rota"])
        self.motor.ciclo_evaluacion()  # no debe lanzar
        self.assertEqual(self.motor._errores_ciclo, 1)


if __name__ == "__main__":
    unittest.main()
