"""
motor_reglas.py - Motor de Reglas reactivo, no bloqueante y multi-condición para Micro-PLC.
Proyecto: Ena-creaccion / Ena-receptor
"""
import time
import ujson
import gc
from hal import HardwareManager


class MotorReglas:
    """
    Motor de ejecución de reglas para MicroPython con soporte para:
    - Reglas simples: [r_id, p_in, op, val, p_out, act, t_type, t_ms]
    - Reglas multi-condición (AND/OR):
      [r_id, [[p1, op1, val1], [p2, op2, val2], ...], logica_and_or, 0, p_out, act, t_type, t_ms]
      (logica_and_or: 0 o "AND" para conjunción, 1 o "OR" para disyunción)
    """

    def __init__(self, hal: HardwareManager = None):
        self.hal = hal if hal is not None else HardwareManager()
        self.reglas = []
        self.estado_timers = {}

    def cargar_reglas(self, trama_bytes):
        """
        Parsea una trama JSON con la lista de reglas e inicializa el hardware
        en la capa HAL de manera segura.
        """
        datos = ujson.loads(trama_bytes)
        self.reglas = datos.get("r", [])
        self.estado_timers.clear()

        # Inicialización de hardware explícito si la trama incluye "hw"
        hw_lista = datos.get("hw", [])
        for item in hw_lista:
            tipo = item.get("tipo", "").lower()
            pin = item.get("pin")
            if tipo in ["dht22", "dht11"]:
                self.hal.asegurar_dht(pin, es_dht22=(tipo == "dht22"))
            elif tipo in ["oled", "ssd1306"]:
                self.hal.asegurar_oled(
                    sda_pin=item.get("sda", 21),
                    scl_pin=item.get("scl", 22),
                    ancho=item.get("ancho", 128),
                    alto=item.get("alto", 64)
                )

        for r in self.reglas:
            # Estructura: [r_id, cond_spec, op_logica, val, p_out, act, t_type, t_ms]
            r_id = r[0]
            cond_spec = r[1]
            op_logica = r[2]
            val = r[3]
            p_out = r[4]

            self.estado_timers[r_id] = [time.ticks_ms(), False]

            # Inicializar salida
            if p_out is not None and p_out >= 0:
                self.hal.asegurar_pin_salida(p_out)

            # Inicializar entradas:
            # Caso A: Multi-condición (lista de sub-condiciones)
            if isinstance(cond_spec, list) and len(cond_spec) > 0 and isinstance(cond_spec[0], (list, tuple)):
                for sub in cond_spec:
                    sub_p = sub[0]
                    sub_val = sub[2]
                    pin_num = sub_p[0] if isinstance(sub_p, (list, tuple)) else sub_p
                    if pin_num is not None and pin_num >= 0:
                        es_adc = (sub_val > 1) and (pin_num not in self.hal.sensores_dht)
                        self.hal.asegurar_pin_entrada(pin_num, es_adc=es_adc)

            # Caso B: Regla simple tradicional
            elif cond_spec is not None and cond_spec >= 0 and op_logica != 4:
                es_adc = (val > 1) and (cond_spec not in self.hal.sensores_dht)
                self.hal.asegurar_pin_entrada(cond_spec, es_adc=es_adc)

        gc.collect()

    def _evaluar_condicion_simple(self, p_spec, op, val):
        """Evalúa un único sensor o canal contra un umbral."""
        if op == 4:
            return True

        if isinstance(p_spec, (list, tuple)):
            pin_num = p_spec[0]
            sub_canal = p_spec[1] if len(p_spec) > 1 else None
        else:
            pin_num = p_spec
            sub_canal = None

        if pin_num is None or pin_num < 0:
            return False

        lectura = self.hal.leer_sensor(pin_num, sub_canal)

        if op == 0: return lectura == val
        if op == 1: return lectura != val
        if op == 2: return lectura > val
        if op == 3: return lectura < val
        return False

    def _evaluar_condicion(self, cond_spec, op_o_logica, val):
        """
        Evalúa la condición general de la regla:
        - Soporta evaluación compuesta AND/OR si cond_spec es lista de subcondiciones.
        - Soporta evaluación simple tradicional.
        """
        # Caso 1: Multi-condición
        if isinstance(cond_spec, list) and len(cond_spec) > 0 and isinstance(cond_spec[0], (list, tuple)):
            # 0 o "AND" = Conjunción (todas deben cumplirse)
            es_and = (op_o_logica in [0, "AND", "and"])
            if es_and:
                for sc in cond_spec:
                    if not self._evaluar_condicion_simple(sc[0], sc[1], sc[2]):
                        return False
                return True
            else:
                # Disyunción OR (al menos una debe cumplirse)
                for sc in cond_spec:
                    if self._evaluar_condicion_simple(sc[0], sc[1], sc[2]):
                        return True
                return False

        # Caso 2: Condición simple tradicional
        return self._evaluar_condicion_simple(cond_spec, op_o_logica, val)

    def ciclo_evaluacion(self):
        """
        Ciclo de escaneo principal. Debe ejecutarse periódicamente dentro
        del bucle principal (while True) sin bloqueos.
        """
        ahora = time.ticks_ms()
        for r in self.reglas:
            r_id, cond_spec, op_logica, val, p_out, act, t_type, t_ms = r
            condicion_cumplida = self._evaluar_condicion(cond_spec, op_logica, val)
            t_inicio, t_activo = self.estado_timers.get(r_id, [ahora, False])

            # Tipo 0: Inmediato (Combinacional directo)
            if t_type == 0:
                self.hal.aplicar_salida(p_out, act if condicion_cumplida else (0 if act == 1 else 1))

            # Tipo 1: Retardo al encendido (TON)
            elif t_type == 1:
                if condicion_cumplida:
                    if not t_activo:
                        self.estado_timers[r_id] = [ahora, True]
                    elif time.ticks_diff(ahora, t_inicio) >= t_ms:
                        self.hal.aplicar_salida(p_out, act)
                else:
                    self.estado_timers[r_id] = [0, False]
                    self.hal.aplicar_salida(p_out, 0)

            # Tipo 2: Pulso Monoestable (PULSE)
            elif t_type == 2:
                if condicion_cumplida and not t_activo:
                    self.hal.aplicar_salida(p_out, act)
                    self.estado_timers[r_id] = [ahora, True]
                elif t_activo:
                    if time.ticks_diff(ahora, t_inicio) >= t_ms:
                        self.hal.aplicar_salida(p_out, 0)
                        if not condicion_cumplida:  # Rearme al cesar condición
                            self.estado_timers[r_id] = [0, False]

            # Tipo 3: Oscilador Periódico Simétrico (BLINK)
            elif t_type == 3:
                if condicion_cumplida:
                    if time.ticks_diff(ahora, t_inicio) >= t_ms:
                        nuevo_estado = not t_activo
                        self.estado_timers[r_id] = [ahora, nuevo_estado]
                        self.hal.aplicar_salida(p_out, 1 if nuevo_estado else 0)
                else:
                    self.estado_timers[r_id] = [ahora, False]
                    self.hal.aplicar_salida(p_out, 0)

            # Tipo 4: Latido de Vida Industrial (HEARTBEAT)
            elif t_type == 4:
                if condicion_cumplida:
                    diferencia = time.ticks_diff(ahora, t_inicio)
                    if diferencia >= t_ms:
                        t_inicio = ahora
                        self.estado_timers[r_id][0] = ahora
                        diferencia = 0

                    if (0 <= diferencia < 80) or (180 <= diferencia < 260):
                        self.hal.aplicar_salida(p_out, 1)
                    else:
                        self.hal.aplicar_salida(p_out, 0)
                else:
                    self.hal.aplicar_salida(p_out, 0)
                    self.estado_timers[r_id] = [ahora, False]
