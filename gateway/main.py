"""
main.py - Firmware para Gateway bidireccional ESP-NOW a Serial/USB (MicroPython).

Dispositivo: Gateway ESP32
Proyecto: Ena-creaccion

El gateway es un tubo tonto a propósito: no interpreta ni reescribe el
contenido de las tramas. Reenviar los bytes tal cual permite que el nodo
verifique la firma HMAC que calculó la Raspberry Pi y evita que un gateway
comprometido pueda alterar órdenes.

Protocolo del puerto serie (una orden por línea):

    MAC|{"cmd":"tx_begin",...}      formato actual (payload crudo)
    {"node":"MAC","payload":{...}}  formato heredado, se re-serializa

Hacia el host cada trama de radio se publica como:

    {"from":"MAC","data":{...}}
"""
import network
import espnow
import machine
import sys

try:
    import ujson
except ImportError:
    import json as ujson

import select
import time

# ESP-NOW admite 250 bytes de payload; el host firma por debajo de 240.
MAX_PAYLOAD = 250
MAX_LINEA = 1024

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
    """Registra un nodo destino con política LRU para no desbordar la tabla."""
    if mac_bytes in peers_registrados:
        peers_registrados.remove(mac_bytes)
        peers_registrados.append(mac_bytes)
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
    """Convierte 'AA:BB:CC:DD:EE:FF' en bytes; lanza ValueError si no es válida."""
    partes = mac_str.strip().split(":")
    if len(partes) != 6:
        raise ValueError("formato_mac")
    return bytes(int(parte, 16) for parte in partes)


def emitir(objeto):
    sys.stdout.write(ujson.dumps(objeto) + "\n")


def procesar_orden(linea):
    """
    Interpreta una línea del host y la transmite por radio.

    No modifica el payload: en el formato con tubería se envía exactamente el
    texto recibido, que es sobre lo que el nodo comprueba la firma.
    """
    if "|" in linea:
        mac_texto, payload_texto = linea.split("|", 1)
        payload_texto = payload_texto.strip()
        if not payload_texto:
            emitir({"error": "payload_vacio"})
            return
    else:
        # Formato heredado {"node": ..., "payload"/"reglas"/"cmd": ...}
        try:
            paquete = ujson.loads(linea)
            mac_texto = paquete["node"]
            payload_obj = paquete.get("payload", paquete.get("reglas", paquete.get("cmd")))
            payload_texto = ujson.dumps(payload_obj)
        except Exception as err:
            emitir({"error": "orden_invalida", "detalle": str(err)})
            return

    try:
        mac_destino = mac_texto_a_bytes(mac_texto)
    except Exception:
        emitir({"error": "mac_invalida", "detalle": mac_texto})
        return

    bytes_payload = payload_texto.encode("utf-8")
    if len(bytes_payload) > MAX_PAYLOAD:
        emitir({
            "error": "payload_muy_grande",
            "max": MAX_PAYLOAD,
            "bytes": len(bytes_payload),
        })
        return

    if not registrar_peer(mac_destino):
        return

    try:
        ack = e.send(mac_destino, bytes_payload)
        emitir({
            "sistema": "tx",
            "node": mac_texto.strip().upper(),
            "ok": bool(ack),
            "bytes": len(bytes_payload),
        })
    except Exception as err:
        emitir({"error": "gateway_error", "detalle": str(err)})


# 2. Monitoreo del puerto serie no bloqueante
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

emitir({"sistema": "gateway_listo", "canal": 1, "max": MAX_PAYLOAD})

buffer_serie = ""

while True:
    wdt.feed()
    hubo_actividad = False

    # --- A. Paquetes entrantes desde los nodos (Radio -> Serie) ---
    # Se drenan varios por vuelta para no acumular retraso con muchas balizas.
    for _ in range(4):
        try:
            resultado = e.recv(0)
        except OSError:
            break
        if not resultado:
            break
        host, msg = resultado[0], resultado[1]
        if not msg:
            break
        hubo_actividad = True
        mac_origen = ":".join("%02X" % b for b in host)
        try:
            # Se inserta el JSON tal cual llegó: sin re-serializar.
            sys.stdout.write(
                '{"from":"%s","data":%s}\n' % (mac_origen, msg.decode("utf-8"))
            )
        except UnicodeError:
            sys.stdout.write('{"from":"%s","error":"trama_corrupta"}\n' % mac_origen)
        except Exception:
            sys.stdout.write('{"from":"%s","error":"trama_ilegible"}\n' % mac_origen)

    # --- B. Órdenes entrantes desde el Host (Serie -> Radio) ---
    if poll.poll(0):
        while poll.poll(0):
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch == "\n":
                linea = buffer_serie.strip()
                buffer_serie = ""
                if linea:
                    hubo_actividad = True
                    procesar_orden(linea)
            elif ch != "\r":
                buffer_serie += ch
                if len(buffer_serie) > MAX_LINEA:
                    buffer_serie = ""
                    emitir({"error": "linea_muy_larga"})

    if not hubo_actividad:
        time.sleep_ms(2)
