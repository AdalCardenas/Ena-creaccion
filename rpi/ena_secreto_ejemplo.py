# ena_secreto_ejemplo.py - Copiar como `ena_secreto.py`.
#
# Debe contener exactamente el mismo valor que `secreto.py` en cada nodo.
# Alternativa sin archivo: variable de entorno ENA_SECRET.
#
# Generar una clave nueva:
#   python -c "import secrets; print(secrets.token_hex(32))"

CLAVE = b"cambia-esto-por-64-caracteres-hexadecimales-aleatorios"
