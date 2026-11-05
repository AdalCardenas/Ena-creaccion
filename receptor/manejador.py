"""
manejador.py - Interpretación de tramas del nodo Ena (sin hardware de radio).

Proyecto: Ena-creaccion / Ena-receptor (ESP8266 / ESP-01S)

Este módulo contiene toda la lógica que antes vivía dentro del bucle de
`main.py`. Al separarla del ESP-NOW y del arranque del chip se puede probar en
la Raspberry Pi con un motor de reglas real y un HAL simulado.

Contrato:
- `procesar(bytes)` recibe exactamente lo que llegó por radio y devuelve la
  lista de tramas de respuesta ya serializadas (puede ser vacía).
- Ningún paquete desconocido o mal formado modifica las reglas activas: antes
  cualquier mensaje sin estructura se guardaba en flash como si fuera una
  configuración, lo que permitía borrar el equipo con basura.
"""
try:
    import ujson as ujson
except ImportError:  # CPython
    import json as ujson

try:
    from . import protocolo
    from . import reloj
except ImportError:  # ejecución plana en el dispositivo
    import protocolo
    import reloj

PIN_LED_IDENT = 2
PARPADEO_MS = 150
IDENTIFICACION_MAX_MS = 60000


class ManejadorPaquetes:
    """Despacha las tramas del gateway al motor de reglas y al HAL."""

    def __init__(self, motor, mac_local, clave=None, persistir=None,
                 chip="esp8266"):
        self.motor = motor
        self.mac = str(mac_local).upper()
        self.clave = clave
        self.persistir = persistir
        self.chip = chip
        self.gestor = protocolo.GestorTransacciones()
        self.ultimo_contador = 0
        self.identificacion_hasta = 0
        self._salida_contador = 0
        self.errores_auth = 0

    # =====================================================================
    # ESTADO PERSISTENTE
    # =====================================================================

    def restaurar_contador(self, contador):
        """Recupera el último contador aceptado desde la flash."""
        try:
            self.ultimo_contador = int(contador or 0)
        except (TypeError, ValueError):
            self.ultimo_contador = 0

    # =====================================================================
    # ENTRADA DE DATOS
    # =====================================================================

    def procesar(self, crudo):
        """Procesa una trama y devuelve las respuestas serializadas."""
        if not crudo:
            return []

        try:
            paquete = ujson.loads(crudo)
        except (ValueError, OSError):
            return [self._respuesta({"st": "error", "detalle": "json_invalido"})]

        if not isinstance(paquete, dict):
            return [self._respuesta({"st": "error", "detalle": "trama_no_objeto"})]

        # 1. Autenticidad e integridad antes de tocar cualquier estado.
        if self.clave:
            ok, motivo, contador = protocolo.verificar(
                self.clave, self.mac, paquete, self.ultimo_contador
            )
            if not ok:
                self.errores_auth += 1
                print(f"[Nodo] Trama rechazada: {motivo}")
                return [self._respuesta({
                    "st": "error",
                    "detalle": "auth_fail",
                    "motivo": motivo,
                    "id": paquete.get("id"),
                })]
            self.ultimo_contador = contador

        # 2. Transacciones abandonadas hace mucho tiempo.
        if self.gestor.activa() and self.gestor.expirada(reloj.ahora_ms()):
            print("[Nodo] Transaccion abandonada, se descarta.")
            self.gestor.descartar()

        # 3. Despacho estricto.
        try:
            return self._despachar(paquete)
        except protocolo.ErrorProtocolo as err:
            print(f"[Nodo] Trama rechazada: {err}")
            return [self._respuesta({
                "st": "error", "detalle": str(err), "id": paquete.get("id")
            })]
        except Exception as err:  # el nodo nunca debe reiniciarse por una trama
            print(f"[Nodo] Error procesando mensaje: {err}")
            return [self._respuesta({
                "st": "error", "detalle": "interno", "id": paquete.get("id")
            })]

    def _despachar(self, paquete):
        comando = paquete.get("cmd")

        if comando == "tx_begin":
            return self._tx_begin(paquete)
        if comando == "tx_chunk":
            return self._tx_chunk(paquete)
        if comando == "tx_commit":
            return self._tx_commit(paquete)
        if comando == "tx_abort":
            return self._tx_abort(paquete)
        if comando == "identificar":
            return self._identificar(paquete)
        if comando == "display":
            return self._display(paquete)
        if comando == "ping":
            return [self._respuesta({"st": "ok", "cmd": "ping"})]

        # Compatibilidad: {"r": [...]} reemplaza la configuración completa.
        if comando is None and "r" in paquete:
            return self._reemplazar(paquete)

        # Cualquier otra cosa se rechaza sin tocar las reglas.
        print(f"[Nodo] Comando desconocido: {comando!r}")
        return [self._respuesta({
            "st": "error",
            "detalle": "comando_desconocido",
            "cmd": comando
        })]

    # =====================================================================
    # CONFIGURACIÓN POR LOTES
    # =====================================================================

    def _tx_begin(self, paquete):
        tx_id = paquete.get("id")
        total = paquete.get("t", 0)
        info = self.gestor.iniciar(tx_id, total, paquete.get("hw"), reloj.ahora_ms())
        return [self._respuesta({
            "st": "ok", "cmd": "tx_begin", "id": tx_id, "t": info["total"]
        })]

    def _tx_chunk(self, paquete):
        tx_id = paquete.get("id")
        cantidad = self.gestor.agregar(tx_id, paquete.get("k"), paquete.get("r", []))
        return [self._respuesta({
            "st": "ok", "cmd": "tx_chunk", "id": tx_id,
            "k": paquete.get("k"), "n_reglas": cantidad
        })]

    def _tx_commit(self, paquete):
        tx_id = paquete.get("id")
        reglas, hw = self.gestor.confirmar(tx_id)
        aplicadas = self._aplicar(reglas, hw)
        return [self._respuesta({
            "st": "ok", "cmd": "tx_commit", "id": tx_id, "reglas": aplicadas
        })]

    def _tx_abort(self, paquete):
        tx_id = paquete.get("id")
        self.gestor.abortar(tx_id)
        return [self._respuesta({"st": "ok", "cmd": "tx_abort", "id": tx_id})]

    def _reemplazar(self, paquete):
        reglas, motivo = protocolo.validar_reglas(paquete.get("r"))
        if motivo:
            return [self._respuesta({
                "st": "error", "detalle": motivo, "id": paquete.get("id")
            })]
        hw, motivo_hw = protocolo.validar_hw(paquete.get("hw"))
        if motivo_hw:
            return [self._respuesta({
                "st": "error", "detalle": motivo_hw, "id": paquete.get("id")
            })]
        aplicadas = self._aplicar(reglas, hw)
        return [self._respuesta({
            "st": "ok", "cmd": "rules", "reglas": aplicadas,
            "id": paquete.get("id")
        })]

    def _aplicar(self, reglas, hw):
        self.motor.aplicar_reglas(reglas, hw=hw)
        if not reglas:
            self.motor.hal.apagar_todas_las_salidas()

        if self.persistir is not None:
            try:
                self.persistir(reglas, hw, self.ultimo_contador)
            except Exception as err:
                print(f"[Nodo] Aviso al guardar reglas: {err}")

        print(f"[Nodo] Reglas aplicadas ({len(reglas)} activas).")
        return len(reglas)

    # =====================================================================
    # COMANDOS DIRECTOS
    # =====================================================================

    def _identificar(self, paquete):
        try:
            duracion = int(paquete.get("duracion_ms", 3000))
        except (TypeError, ValueError):
            duracion = 3000
        duracion = max(0, min(duracion, IDENTIFICACION_MAX_MS))
        self.identificacion_hasta = reloj.sumar(reloj.ahora_ms(), duracion)
        return [self._respuesta({
            "st": "ok", "cmd": "identificar", "id": paquete.get("id"),
            "duracion_ms": duracion
        })]

    def _display(self, paquete):
        lineas = paquete.get("lineas", [])
        if not isinstance(lineas, (list, tuple)):
            raise protocolo.ErrorProtocolo("lineas_invalidas")
        if len(lineas) > 6:
            lineas = list(lineas)[:6]
        ok = self.motor.hal.mostrar_en_oled([str(x) for x in lineas])
        return [self._respuesta({
            "st": "ok" if ok else "error", "cmd": "display",
            "id": paquete.get("id"), "detalle": None if ok else "sin_pantalla"
        })]

    # =====================================================================
    # BALIZA Y LED DE IDENTIFICACIÓN
    # =====================================================================

    def construir_baliza(self):
        """Baliza de autodescubrimiento; firmada si hay secreto configurado."""
        reglas_activas = len(self.motor.reglas)
        datos = {
            "beacon": "ena_node",
            "v": protocolo.VERSION,
            "chip": self.chip,
            "status": "listo" if reglas_activas else "nuevo",
            "reglas": reglas_activas,
            "auth": 1 if self.clave else 0
        }
        return self._respuesta(datos)

    def tick(self):
        """Aplica el parpadeo del LED cuando se pidió identificar el nodo."""
        if not self.identificacion_hasta:
            return
        ahora = reloj.ahora_ms()
        if reloj.diferencia(ahora, self.identificacion_hasta) < 0:
            fase = 1 if ((ahora // PARPADEO_MS) % 2) else 0
            self.motor.hal.aplicar_salida(PIN_LED_IDENT, fase)
        else:
            self.identificacion_hasta = 0
            self.motor.hal.aplicar_salida(PIN_LED_IDENT, 0)

    def apagar_led_identificacion(self):
        self.identificacion_hasta = 0
        self.motor.hal.aplicar_salida(PIN_LED_IDENT, 0)

    # =====================================================================
    # SALIDA
    # =====================================================================

    def _siguiente_contador(self):
        self._salida_contador += 1
        return int(reloj.ahora_ms()) * 1000 + self._salida_contador

    def _respuesta(self, datos):
        """Serializa (y firma, si aplica) una respuesta hacia el gateway."""
        if self.clave:
            datos = protocolo.firmar(self.clave, self.mac,
                                     self._siguiente_contador(), datos)
        try:
            return ujson.dumps(datos).encode("utf-8")
        except Exception as err:
            print(f"[Nodo] No se pudo serializar la respuesta: {err}")
            return b'{"st":"error","detalle":"serializacion"}'
