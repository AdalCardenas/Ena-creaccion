# secreto_ejemplo.py - Copiar como `secreto.py` en el nodo ESP8266.
#
# El mismo valor debe existir en la Raspberry Pi (rpi/ena_secreto.py o la
# variable de entorno ENA_SECRET). Sin este archivo el nodo funciona en modo
# abierto: acepta cualquier trama del canal 1 y lo anuncia en su baliza.
#
# Generar una clave nueva:
#   python -c "import secrets; print(secrets.token_hex(32))"
#
# El gateway NO conoce la clave: sólo transporta bytes, así que un gateway
# comprometido no permite falsificar órdenes.

CLAVE = b"cambia-esto-por-64-caracteres-hexadecimales-aleatorios"

# Opcional: forzar la MAC usada para firmar, si el firmware reporta una
# distinta de la que ve el gateway (por ejemplo con AP y STA activos).
# MAC_FORZADA = "A0:20:A6:11:22:33"
