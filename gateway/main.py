"""
main.py - Firmware para Gateway bidireccional ESP-NOW a Serial/USB (MicroPython).
Dispositivo: Gateway ESP32
Proyecto: Ena-creaccion
"""
import network
import espnow
import machine
import sys
import ujson
import select
import time
import gc

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

e = espnow.ESPNow()
e.active(True)

# Watchdog Timer por hardware (5 segundos): si el chip se bloquea, se reinicia solo
wdt = machine.WDT(timeout=5000)

# Registro de peers en memoria para no exceder los 20 máximos de hardware ESP-NOW
peers_registrados = []
MAX_PEERS = 18  # Margen de seguridad sobre el límite de 20

def registrar_peer(mac_bytes):
    """Registra dinámicamente un nodo destino evitando desbordar la tabla de peers."""
    if mac_bytes in peers_registrados:
        return True
    try:
        if len(peers_registrados) >= MAX_PEERS:
            peer_antiguo = peers_registrados.pop(0)
            try:
                e.del_peer(peer_antiguo)
            except OSError:
                pass
        e.add_peer(mac_bytes)
        peers_registrados.append(mac_bytes)
        return True
    except Exception as err:
        sys.stdout.write(f'{{"error":"peer_error","detalle":"{str(err)}"}}\n')
        return False

def mac_texto_a_bytes(mac_str):
    """Convierte dirección MAC en formato texto 'AA:BB:CC:DD:EE:FF' a bytes."""
    return bytes(int(b, 16) for b in mac_str.split(':'))

# 2. Monitoreo del puerto serie no bloqueante
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

sys.stdout.write('{"sistema":"gateway_listo","canal":1}\n')

buffer_serie = ""

while True:
    wdt.feed()
    hubo_actividad = False

    # --- A. Paquetes entrantes desde los nodos (Radio -> Serie) ---
    host, msg = e.recv(0)
    if msg:
        hubo_actividad = True
        mac_origen = ":".join(f"{b:02X}" for b in host)
        try:
            msg_str = msg.decode("utf-8")
            sys.stdout.write(f'{{"from":"{mac_origen}","data":{msg_str}}}\n')
        except UnicodeError:
            sys.stdout.write(f'{{"from":"{mac_origen}","error":"trama_corrupta"}}\n')

    # --- B. Comandos entrantes desde el Host (Serie -> Radio) - 100% No Bloqueante ---
    if poll.poll(0):
        while poll.poll(0):
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch == '\n':
                linea = buffer_serie.strip()
                buffer_serie = ""
                if linea:
                    hubo_actividad = True
                    try:
                        paquete = ujson.loads(linea)
                        mac_destino = mac_texto_a_bytes(paquete["node"])

                        if registrar_peer(mac_destino):
                            payload_obj = paquete.get("payload", paquete.get("reglas", paquete.get("cmd")))
                            payload_bytes = ujson.dumps(payload_obj).encode('utf-8')

                            if len(payload_bytes) > 250:
                                sys.stdout.write('{"error":"payload_muy_grande","max":250}\n')
                            else:
                                ack = e.send(mac_destino, payload_bytes)
                                sys.stdout.write(f'{{"sistema":"comando_transmitido","ack":{ "true" if ack else "false" }}}\n')
                    except Exception as err:
                        sys.stdout.write(f'{{"error":"gateway_error","detalle":"{str(err)}"}}\n')
            elif ch != '\r':
                buffer_serie += ch
                # Si llega basura que excede 500 caracteres sin salto de línea, vaciar buffer
                if len(buffer_serie) > 500:
                    buffer_serie = ""

    # Si no hubo actividad en este ciclo, ceder brevemente la CPU
    if not hubo_actividad:
        time.sleep_ms(2)
