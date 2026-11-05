# Ena-creaccion

Un puente entre el lenguaje humano y el mundo físico: conecta sensores y actuadores a un ESP32, dile a Hermes Agent qué quieres que haga y deja que la IA configure una red de micro-PLCs autónomos por radio sin que tengas que escribir una sola línea de código ni tocar una dirección MAC.

## Cómo está armado

| Capa | Carpeta | Qué hace |
|---|---|---|
| Agente | `rpi/hermes_tools.py` | Traduce lenguaje natural a llamadas de herramienta (function calling). |
| Demonio | `rpi/ena_service.py` | API REST local (`http://127.0.0.1:8765`) + puerto serie del gateway. |
| Registro | `rpi/device_registry.py` | "Bautiza" MACs con alias humanos y guarda el tipo de cada pin. |
| Compilador | `rpi/rule_compiler.py` | Convierte intenciones en la matriz de 8 enteros del micro-PLC. |
| Gateway | `gateway/main.py` | Puente ESP-NOW ↔ USB (canal 1). No interpreta el contenido. |
| Nodo | `receptor/` | Micro-PLC autónomo: motor de reglas, HAL, OLED, DHT, PWM. |

## Seguridad

El canal de radio y la API local están cerrados por defecto:

1. **Token de API.** Se genera solo en `rpi/ena_config.json` al primer arranque.
   Toda petición debe llevar `Authorization: Bearer <token>`; `hermes_tools` lo
   lee del archivo, así que el agente no lo maneja. `GET /api/ping` es la única
   ruta abierta (para saber si el demonio vive).
2. **Tramas firmadas.** Cada orden viaja con `n` (contador monótono) y `h`
   (HMAC-SHA256 truncado a 64 bits) calculado sobre `ena2|MAC|n|cuerpo_canónico`.
   El nodo rechaza tramas sin firma, con MAC ajena, alteradas o repetidas.
   Las balizas y las respuestas del nodo también se firman y la Pi las verifica.
3. **Secreto compartido.** Debe existir el mismo valor en `rpi/ena_secreto.py`
   y en `receptor/secreto.py` (o en la variable `ENA_SECRET`). Hay plantillas
   `*_ejemplo.py`. Sin secreto, el nodo funciona en modo abierto y lo anuncia
   con `"auth":0`; la Pi, si tiene secreto, descarta esas balizas salvo que se
   defina `ENA_PERMITIR_SIN_FIRMA=1` (sólo para laboratorio).

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # generar una clave
```

## Protocolo de tramas (v2)

- Host → gateway: una orden por línea, `MAC|{"cmd":...}`. El gateway reenvía el
  payload **tal cual** para no romper la firma.
- Configuraciones grandes: `tx_begin` (total de lotes + `hw`) → `tx_chunk`
  (hasta ~200 bytes de reglas) → `tx_commit`. El nodo sólo aplica al confirmar,
  así que un corte a mitad no deja media configuración. Esto elimina el límite
  práctico de 3-5 reglas impuesto por los 250 bytes de ESP-NOW.
- Cada pin declara su tipo en `hw` (`digital_in`, `adc`, `dht22`, `digital_out`,
  `oled`); el nodo ya no adivina el modo del pin a partir del umbral.

## Pruebas

```bash
cd rpi
python -m unittest discover -s . -p "test*.py" -v
```

Las pruebas corren sin hardware: `rpi/tests/sim/` simula `machine` y `framebuf`,
de modo que el firmware del nodo (`receptor/`) se ejercita de verdad —
temporizadores, ADC, DHT, OLED, autenticación, lotes y rechazos — y la API se
prueba de extremo a extremo alimentando las tramas enviadas a un nodo simulado.
