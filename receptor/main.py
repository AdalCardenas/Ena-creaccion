"""
main.py - Firmware del Nodo Receptor / Micro-PLC Autónomo (MicroPython).

Dispositivo: ESP8266 ESP-01S (8 pines) / NodeMCU / Wemos D1 Mini
Proyecto: Ena-creaccion / Ena-receptor

Este archivo sólo se ocupa del arranque: radio, watchdog, flash y bucle. Toda
la interpretación de tramas vive en `manejador.py` (probado en la Raspberry Pi)
y el motor de control en `motor_reglas.py`.

Autenticación: si existe `secreto.py` junto a este archivo, todas las tramas
recibidas deben venir firmadas con ese secreto. Si no existe, el nodo funciona
en modo abierto (útil para pruebas) y lo anuncia en su baliza con "auth":0.
"""
import espnow
import network
import machine
import os

try:
    import ujson as ujson
except ImportError:  # CPython / pruebas
    import json as ujson

import gc

import reloj
from motor_reglas import MotorReglas
from manejador import ManejadorPaquetes

# Secreto compartido opcional (mismo valor que en la Raspberry Pi).
try:
    from secreto import CLAVE
except ImportError:
    CLAVE = None

try:
    from secreto import MAC_FORZADA
except ImportError:
    MAC_FORZADA = None

ARCHIVO_REGLAS = "reglas.json"
ARCHIVO_TEMP = "reglas.tmp"
BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"
PIN_LED_IDENT = 2  # LED azul integrado en GPIO 2 (Active-LOW manejado por HAL)

# 1. Configurar radio en modo Estación (STA) y fijar sintetizador en Canal 1
sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.disconnect()

# Dejar AP activo en canal 1 bloquea el hardware del ESP8266 en canal 1
ap = network.WLAN(network.AP_IF)
ap.active(True)
try:
    ap.config(channel=1)
except Exception as err:
    print("[Radio] Aviso canal AP:", err)

# 2. Inicializar ESP-NOW y registrar broadcast para balizas
e = espnow.ESPNow()
e.active(True)
try:
    e.add_peer(BROADCAST_MAC)
    print("[ESP-NOW] Activo en Canal 1. Peer broadcast registrado.")
except Exception as err:
    print("[ESP-NOW] Aviso al registrar broadcast:", err)


def _mac_local():
    """MAC de la interfaz STA: es la que ve el gateway en las tramas."""
    if MAC_FORZADA:
        return str(MAC_FORZADA).upper()
    try:
        mac = sta.config("mac")
        return ":".join("%02X" % b for b in mac)
    except Exception as err:
        print("[Radio] No se pudo leer la MAC:", err)
        return "00:00:00:00:00:00"


# 3. Inicializar Motor de Reglas (y HAL)
motor = MotorReglas()


def persistir_reglas(reglas, hw, contador):
    """
    Guarda la configuración en flash de forma atómica.

    Se escribe primero un temporal y luego se renombra: si se corta la energía
    a mitad de escritura, el archivo anterior sigue siendo válido.
    """
    datos = {"v": 2, "r": reglas, "hw": hw or [], "n": int(contador)}
    texto = ujson.dumps(datos)
    try:
        with open(ARCHIVO_TEMP, "w") as archivo:
            archivo.write(texto)
            archivo.flush()
        try:
            os.remove(ARCHIVO_REGLAS)
        except OSError:
            pass
        os.rename(ARCHIVO_TEMP, ARCHIVO_REGLAS)
    except Exception as err:
        print("[Flash] Aviso al guardar reglas:", err)
    gc.collect()


# 4. Recuperar la última configuración persistida
manejador = ManejadorPaquetes(
    motor,
    mac_local=_mac_local(),
    clave=CLAVE,
    persistir=persistir_reglas,
)

try:
    with open(ARCHIVO_REGLAS, "r") as archivo:
        texto_guardado = archivo.read()
    guardado = ujson.loads(texto_guardado)
    if isinstance(guardado, dict):
        manejador.restaurar_contador(guardado.get("n"))
        if motor.cargar_reglas(texto_guardado):
            print(f"[Flash] Reglas previas cargadas ({len(motor.reglas)} activas).")
except OSError:
    print("[Flash] Sin reglas previas. Esperando configuración de Hermes...")
except Exception as err:
    # Un archivo corrupto no debe impedir que el nodo arranque.
    print("[Flash] reglas.json ilegible, se ignora:", err)

# Asegurar LED de identificación apagado al inicio
motor.hal.asegurar_pin_salida(PIN_LED_IDENT)
manejador.apagar_led_identificacion()

# 5. Watchdog Hardware (compatible con ESP8266 sin argumento timeout)
try:
    wdt = machine.WDT()
    print("[WDT] Watchdog hardware activo.")
except Exception as err:
    print("[WDT] Aviso iniciando Watchdog:", err)
    wdt = None

print("[Nodo] ESP-01S en línea y escuchando en Canal 1...")
if CLAVE:
    print("[Nodo] Autenticacion activa (HMAC-SHA256).")
else:
    print("[Nodo] AVISO: sin secreto.py, el nodo acepta tramas sin firmar.")

ultima_baliza = 0
ultimo_gateway = None


def _recibir():
    """Lee una trama de radio sin bloquear; tolera firmware que retorna None."""
    try:
        resultado = e.recv(0)
    except OSError:
        return None, None
    if not resultado:
        return None, None
    return resultado[0], resultado[1]


def _enviar(destino, datos):
    """Envía al gateway registrándolo como peer si es la primera vez."""
    if destino is None:
        return False
    if destino != ultimo_gateway:
        try:
            e.add_peer(destino)
        except OSError:
            pass
    try:
        return e.send(destino, datos)
    except Exception as err:
        print("[Nodo] Error al responder:", err)
        return False


while True:
    if wdt:
        wdt.feed()

    ahora = reloj.ahora_ms()

    # --- A. Baliza de auto-descubrimiento ---
    intervalo = 5000 if not motor.reglas else 30000
    if reloj.diferencia(ahora, ultima_baliza) >= intervalo:
        ultima_baliza = ahora
        gc.collect()  # mantener RAM libre en ESP8266 (~35 KB)
        try:
            baliza = manejador.construir_baliza()
            enviado = e.send(BROADCAST_MAC, baliza)
            print(f"[Nodo] Baliza emitida ({len(baliza)} bytes, enviado={enviado})")
        except Exception as err:
            print("[Nodo] Error al emitir baliza:", err)

    # --- B. Parpadeo de identificación si fue solicitado ---
    manejador.tick()

    # --- C. Tramas entrantes: comandos, reglas o consultas ---
    for _ in range(4):
        host, msg = _recibir()
        if not msg:
            break
        if host:
            ultimo_gateway = host
        for respuesta in manejador.procesar(msg):
            _enviar(host, respuesta)

    # --- D. Ciclo de escaneo del micro-PLC ---
    motor.ciclo_evaluacion()

    # --- E. Scan time controlado (10 ms ~ 100 Hz) ---
    reloj.dormir_ms(10)
