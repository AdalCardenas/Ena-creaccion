"""
reloj.py - Reloj monotónico compatible con MicroPython y CPython.

Proyecto: Ena-creaccion / Ena-receptor

Motivo: el motor de reglas y el manejador de paquetes no deben depender de
`time.ticks_ms()` directamente. En MicroPython existe, en CPython no; y al
aislarlo en este módulo las pruebas pueden sustituir `ahora_ms` por un reloj
ficticio para verificar temporizadores sin dormir el proceso.

El contador de MicroPython (`ticks_ms`) usa 30 bits y desborda; `diferencia()`
reproduce `time.ticks_diff()` incluso en CPython.
"""
try:
    import utime as _t
except ImportError:  # CPython no tiene utime
    import time as _t

MASCARA = 0x3FFFFFFF  # 30 bits, igual que el contador de MicroPython
_MEDIO = 0x20000000


def ahora_ms():
    """Milisegundos monotónicos, con desbordamiento de 30 bits."""
    if hasattr(_t, "ticks_ms"):
        return _t.ticks_ms()
    return int(_t.monotonic() * 1000) & MASCARA


def diferencia(a, b):
    """Devuelve a - b tolerando el desbordamiento de 30 bits (ticks_diff)."""
    if hasattr(_t, "ticks_diff"):
        return _t.ticks_diff(a, b)
    d = (a - b) & MASCARA
    if d & _MEDIO:
        d -= MASCARA + 1
    return d


def sumar(a, ms):
    """Suma milisegundos a un instante (ticks_add)."""
    if hasattr(_t, "ticks_add"):
        return _t.ticks_add(a, ms)
    return (a + ms) & MASCARA


def dormir_ms(ms):
    """Pausa no bloqueante del planificador (sleep_ms)."""
    if hasattr(_t, "sleep_ms"):
        _t.sleep_ms(ms)
    else:
        _t.sleep(ms / 1000.0)
