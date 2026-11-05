"""
util.py - Utilidades compartidas por las pruebas del proyecto Ena.

Proyecto: Ena-creaccion

Carga el firmware del receptor como paquete Python (`ena_nodo`) con el
simulador de `machine` delante en sys.path, y ofrece un reloj ficticio para
verificar los temporizadores sin dormir el proceso.
"""
import importlib
import importlib.util
import os
import sys

DIR_TESTS = os.path.dirname(os.path.abspath(__file__))
RPI = os.path.dirname(DIR_TESTS)
PROYECTO = os.path.dirname(RPI)
RECEPTOR = os.path.join(PROYECTO, "receptor")
LIB_RECEPTOR = os.path.join(RECEPTOR, "lib")
SIM = os.path.join(DIR_TESTS, "sim")

ALIAS_PAQUETE = "ena_nodo"


def preparar_entorno():
    """Deja sys.path listo para importar el receptor con el simulador."""
    for ruta in (RPI, SIM):
        if ruta not in sys.path:
            sys.path.insert(0, ruta)
    if LIB_RECEPTOR not in sys.path:
        sys.path.append(LIB_RECEPTOR)


def cargar_nodo():
    """Importa receptor/ como paquete `ena_nodo` (una sola vez)."""
    preparar_entorno()
    if ALIAS_PAQUETE in sys.modules:
        return sys.modules[ALIAS_PAQUETE]

    spec = importlib.util.spec_from_file_location(
        ALIAS_PAQUETE,
        os.path.join(RECEPTOR, "__init__.py"),
        submodule_search_locations=[RECEPTOR],
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[ALIAS_PAQUETE] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def modulo_nodo(nombre):
    """Importa un submódulo del receptor (`protocolo`, `motor_reglas`, ...)."""
    cargar_nodo()
    return importlib.import_module("%s.%s" % (ALIAS_PAQUETE, nombre))


class RelojFalso(object):
    """Sustituye `reloj.ahora_ms` por un contador controlado."""

    def __init__(self, inicio=1000):
        self.ms = inicio
        self._modulo = None
        self._original = None

    def instalar(self):
        self._modulo = modulo_nodo("reloj")
        self._original = self._modulo.ahora_ms
        self._modulo.ahora_ms = lambda: self.ms
        return self

    def avanzar(self, ms):
        self.ms += ms
        return self.ms

    def fijar(self, ms):
        self.ms = ms
        return self.ms

    def restaurar(self):
        if self._modulo is not None and self._original is not None:
            self._modulo.ahora_ms = self._original
