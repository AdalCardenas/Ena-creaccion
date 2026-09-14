"""
main.py - Firmware para Gateway bidireccional ESP-NOW a Serial/USB (MicroPython).
Dispositivo: Gateway ESP32
Proyecto: Ena-creaccion
"""
import network
import espnow
import sys
import ujson
import select
import time
import gc

# 1. Configurar radio en modo Estación (STA)
sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.disconnect()

# En firmware reciente se puede configurar el canal en STA;
# como fallback se usa la interfaz AP:
try:
    sta.config(channel=1)
except ValueError:
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(channel=1)
    ap.active(False)

e = espnow.ESPNow()
e.active(True)

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

# 2. Monitoreo del puerto serie
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

sys.stdout.write('{"sistema":"gateway_listo","canal":1}\n')

while True:
    hubo_actividad = False

    # --- A. Paquetes entrantes desde los nodos (Radio -> Serie) ---
    host, msg = e.recv(0)
    if msg:
        hubo_actividad = True
        mac_origen = ":".join(f"{b:02X}" for b in host)
        try:
            # Intentar decodificar como texto/json seguro
            msg_str = msg.decode("utf-8")
            sys.stdout.write(f'{{"from":"{mac_origen}","data":{msg_str}}}\n')
        except UnicodeError:
            sys.stdout.write(f'{{"from":"{mac_origen}","error":"trama_corrupta"}}\n')

    # --- B. Comandos entrantes desde el Host (Serie -> Radio) ---
    if poll.poll(0):
        hubo_actividad = True
        linea = sys.stdin.readline().strip()
        if linea:
            try:
                paquete = ujson.loads(linea)
                mac_destino = mac_texto_a_bytes(paquete["node"])

                if registrar_peer(mac_destino):
                    payload_obj = paquete.get("payload", paquete.get("reglas", paquete.get("cmd")))
                    payload_bytes = ujson.dumps(payload_obj).encode('utf-8')
                    
                    if len(payload_bytes) > 250:
                        sys.stdout.write('{"error":"payload_muy_grande","max":250}\n')
                    else:
                        # e.send() devuelve True si el nodo respondió con ACK de radio
                        ack = e.send(mac_destino, payload_bytes)
                        sys.stdout.write(f'{{"sistema":"comando_transmitido","ack":{ "true" if ack else "false" }}}\n')
            except Exception as err:
                sys.stdout.write(f'{{"error":"gateway_error","detalle":"{str(err)}"}}\n')

    # Si no hubo actividad en este ciclo, ceder brevemente la CPU para no saturar núcleos ni WDT
    if not hubo_actividad:
        time.sleep_ms(2)
