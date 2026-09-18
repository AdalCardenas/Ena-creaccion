"""
main.py - Firmware del Nodo Receptor / Micro-PLC Autónomo (MicroPython).
Dispositivo: Nodo Receptor ESP32
Proyecto: Ena-creaccion / Ena-receptor
"""
import espnow
import network
import machine
import time
import os
import ujson
from motor_reglas import MotorReglas

ARCHIVO_REGLAS = "reglas.json"
BROADCAST_MAC = b'\xff\xff\xff\xff\xff\xff'
PIN_LED_IDENT = 2  # LED azul integrado en la mayoría de ESP32 DevKit

# 1. Configurar radio en modo Estación (STA) y fijar canal 1
sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.disconnect()

# Dejar AP activo en canal 1 bloquea el sintetizador de radio del ESP32 en canal 1
ap = network.WLAN(network.AP_IF)
ap.active(True)
try:
    ap.config(channel=1)
except Exception as err:
    print("Aviso canal AP:", err)

# 2. Inicializar ESP-NOW y registrar broadcast para balizas
e = espnow.ESPNow()
e.active(True)
try:
    e.add_peer(BROADCAST_MAC)
    print("ESP-NOW activo. Peer broadcast registrado en canal 1.")
except Exception as err:
    print("Aviso al registrar broadcast:", err)

# LED de identificación física
led_ident = machine.Pin(PIN_LED_IDENT, machine.Pin.OUT)
led_ident.value(0)
tiempo_fin_ident = 0

# 3. Inicializar Motor de Reglas y cargar reglas previas de Flash
motor = MotorReglas()

def persistir_y_cargar_reglas(trama_bytes):
    """Guarda las reglas en la memoria Flash y las aplica al motor."""
    motor.cargar_reglas(trama_bytes)
    try:
        with open(ARCHIVO_REGLAS, "wb") as f:
            f.write(trama_bytes)
    except Exception as err:
        print("Aviso: no se pudo guardar en Flash:", err)

# Al arrancar: recuperar última configuración persistida
tiene_reglas = False
try:
    with open(ARCHIVO_REGLAS, "rb") as f:
        datos_guardados = f.read()
        if datos_guardados:
            motor.cargar_reglas(datos_guardados)
            tiene_reglas = len(motor.reglas) > 0
            print(f"Reglas previas cargadas ({len(motor.reglas)} activas).")
except OSError:
    print("Sin reglas previas en Flash. Esperando configuración...")

# 4. Inicializar Watchdog (3 segundos)
wdt = machine.WDT(timeout=3000)

print("Nodo receptor en línea y escuchando en Canal 1...")

# Control de baliza periódica de auto-descubrimiento
ultimo_beacon = 0

while True:
    wdt.feed()
    ahora = time.ticks_ms()

    # --- A. Baliza de auto-descubrimiento para Hermes Agent ---
    intervalo_beacon = 5000 if not motor.reglas else 30000  # 5s si nuevo/sin reglas, 30s si configurado
    if time.ticks_diff(ahora, ultimo_beacon) >= intervalo_beacon:
        ultimo_beacon = ahora
        beacon_data = ujson.dumps({
            "beacon": "ena_node",
            "status": "nuevo" if not motor.reglas else "listo",
            "reglas": len(motor.reglas)
        }).encode('utf-8')
        try:
            exito = e.send(BROADCAST_MAC, beacon_data)
            print(f"[Nodo] Baliza emitida ({len(beacon_data)} bytes, enviado={exito})")
        except Exception as err:
            print("[Nodo] Error al emitir baliza:", err)

    # --- B. Parpadeo de Identificación Visual si fue solicitado ---
    if tiempo_fin_ident > 0:
        if time.ticks_diff(ahora, tiempo_fin_ident) < 0:
            # Parpadeo rápido (150ms)
            fase = (ahora // 150) % 2
            led_ident.value(fase)
        else:
            tiempo_fin_ident = 0
            led_ident.value(0)

    # --- C. Comprobar si llegó un comando o actualización por radio ---
    host, msg = e.recv(0)
    if msg:
        try:
            paquete = ujson.loads(msg)

            # Comando especial: Identificación física
            if isinstance(paquete, dict) and paquete.get("cmd") == "identificar":
                duracion = paquete.get("duracion_ms", 3000)
                tiempo_fin_ident = time.ticks_add(ahora, duracion)
                try:
                    e.add_peer(host)
                except OSError:
                    pass
                e.send(host, ujson.dumps({"status": "identificando"}).encode('utf-8'))

            # Comando especial: Mostrar texto en pantalla OLED
            elif isinstance(paquete, dict) and paquete.get("cmd") == "display":
                lineas = paquete.get("lineas", [])
                ok = motor.hal.mostrar_en_oled(lineas)
                try:
                    e.add_peer(host)
                except OSError:
                    pass
                e.send(host, ujson.dumps({"status": "display_ok" if ok else "display_error"}).encode('utf-8'))

            # Actualización de reglas de automatización
            elif isinstance(paquete, dict) and "r" in paquete:
                persistir_y_cargar_reglas(msg)
                if not motor.reglas:
                    motor.hal.apagar_todas_las_salidas()
                print(f"Nuevas reglas aplicadas ({len(motor.reglas)} activas).")
                try:
                    e.add_peer(host)
                except OSError:
                    pass
                respuesta = ujson.dumps({"status": "ok", "reglas": len(motor.reglas)}).encode('utf-8')
                e.send(host, respuesta)

            else:
                # Intento de cargar regla si viene en otro formato
                persistir_y_cargar_reglas(msg)

        except Exception as err:
            print("Error procesando mensaje:", err)
            try:
                e.add_peer(host)
            except OSError:
                pass
            respuesta = ujson.dumps({"status": "error", "detalle": str(err)}).encode('utf-8')
            e.send(host, respuesta)

    # --- D. Ciclo de escaneo y temporizadores del micro-PLC ---
    motor.ciclo_evaluacion()

    # --- E. Scan time controlado (10ms ~ 100 Hz) ---
    time.sleep_ms(10)
