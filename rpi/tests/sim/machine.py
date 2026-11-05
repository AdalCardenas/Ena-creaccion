"""
machine.py - Simulador mínimo del módulo `machine` de MicroPython.

Proyecto: Ena-creaccion (pruebas de la capa Raspberry Pi)

Permite ejecutar el HAL y el motor de reglas del ESP8266 en CPython, de modo
que el firmware tenga pruebas reales sin necesidad de hardware.
Sólo cubre lo que usa el proyecto: Pin, ADC, PWM, WDT y SoftI2C.
"""
import time as _time

# Registro de objetos creados, para que las pruebas puedan inspeccionarlos.
REGISTRO_PINES = {}
REGISTRO_PWM = {}
REGISTRO_ADC = {}


def reset():
    """Limpia el estado simulado entre pruebas."""
    REGISTRO_PINES.clear()
    REGISTRO_PWM.clear()
    REGISTRO_ADC.clear()
    SoftI2C.instancias.clear()


class Pin(object):
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3

    def __init__(self, numero, modo=None, pull=None):
        self.numero = numero
        self.modo = modo
        self.pull = pull
        self._valor = 0
        self.historial = []
        REGISTRO_PINES[numero] = self

    def value(self, valor=None):
        if valor is None:
            return self._valor
        self._valor = 1 if valor else 0
        self.historial.append(self._valor)

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def __repr__(self):
        return "Pin(%r, modo=%r, valor=%r)" % (self.numero, self.modo, self._valor)


class ADC(object):
    ATTN_0DB = 0
    ATTN_2_5DB = 1
    ATTN_6DB = 2
    ATTN_11DB = 3

    def __init__(self, canal=0):
        self.canal = canal
        self.valor = 0
        self.atenuacion = None
        REGISTRO_ADC[canal] = self

    def atten(self, valor):
        self.atenuacion = valor

    def read(self):
        return self.valor


class PWM(object):
    def __init__(self, pin, freq=1000, duty=0):
        self.pin = pin
        self.freq = freq
        self._duty = duty
        self._duty_u16 = 0
        self.deinit_llamado = False
        REGISTRO_PWM[getattr(pin, "numero", pin)] = self

    def duty(self, valor=None):
        if valor is None:
            return self._duty
        self._duty = int(valor)

    def duty_u16(self, valor=None):
        if valor is None:
            return self._duty_u16
        self._duty_u16 = int(valor)

    def deinit(self):
        self.deinit_llamado = True


class WDT(object):
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.alimentado = 0

    def feed(self):
        self.alimentado += 1


class SoftI2C(object):
    instancias = []

    def __init__(self, scl=None, sda=None, freq=100000):
        self.scl = scl
        self.sda = sda
        self.freq = freq
        self.escrituras = []
        SoftI2C.instancias.append(self)

    def writeto(self, direccion, datos):
        self.escrituras.append((direccion, bytes(datos)))

    def writevto(self, direccion, listas):
        plano = b""
        for parte in listas:
            if parte:
                plano += bytes(parte)
        self.escrituras.append((direccion, plano))

    def scan(self):
        return [0x3C]
