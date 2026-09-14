"""
motor_reglas.py - Motor de Reglas reactivo y no bloqueante para Micro-PLC autónomo (MicroPython).
Proyecto: Ena-receptor
"""
import machine
import time
import ujson
import gc


class MotorReglas:
    """
    Motor de ejecución de reglas para control de entradas y salidas en MicroPython.

    Estructura esperada por regla en la trama JSON:
    [r_id, p_in, op, val, p_out, act, t_type, t_ms]
      - r_id   (int): Identificador único de la regla.
      - p_in   (int): Pin de entrada GPIO (o None / -1 si op == 4).
      - op     (int): Operador de comparación:
                      0: == (igual)
                      1: != (diferente)
                      2: >  (mayor que)
                      3: <  (menor que)
                      4: Incondicional (siempre True)
      - val    (int): Valor/umbral de comparación (0/1 para digital, > 1 para analógico/ADC).
      - p_out  (int): Pin de salida GPIO.
      - act    (int): Acción a aplicar:
                      0: Apagar (LOW)
                      1: Encender (HIGH)
                      2: Conmutar (Toggle)
      - t_type (int): Tipo de temporizador:
                      0: Inmediato / Combinacional directo
                      1: TON (Retardo a la conexión)
                      2: PULSE (Monoestable / Pulso único)
                      3: BLINK (Oscilador periódico simétrico)
                      4: HEARTBEAT (Latido industrial de doble destello)
      - t_ms   (int): Tiempo en milisegundos para la temporización.
    """

    def __init__(self):
        self.reglas = []
        self.estado_timers = {}
        self.pines_in = {}
        self.pines_out = {}

    def cargar_reglas(self, trama_bytes):
        """
        Carga y parsea una trama JSON con la lista de reglas, inicializando
        el hardware correspondiente de manera segura.
        """
        datos = ujson.loads(trama_bytes)
        self.reglas = datos.get("r", [])
        self.estado_timers.clear()

        for r in self.reglas:
            # Estructura: [r_id, p_in, op, val, p_out, act, t_type, t_ms]
            r_id, p_in, op, val, p_out = r[0], r[1], r[2], r[3], r[4]
            self.estado_timers[r_id] = [time.ticks_ms(), False]

            # Inicialización de salida
            if p_out is not None and p_out not in self.pines_out:
                self.pines_out[p_out] = machine.Pin(p_out, machine.Pin.OUT)
                self.pines_out[p_out].value(0)

            # Inicialización de entrada (omite si es incondicional o pin nulo)
            if op != 4 and p_in is not None and p_in >= 0:
                if p_in not in self.pines_in:
                    if val > 1:
                        adc = machine.ADC(machine.Pin(p_in))
                        if hasattr(adc, 'atten'):
                            adc.atten(machine.ADC.ATTN_11DB)  # Rango completo 0-3.3V en ESP32
                        self.pines_in[p_in] = adc
                    else:
                        self.pines_in[p_in] = machine.Pin(p_in, machine.Pin.IN, machine.Pin.PULL_DOWN)

        gc.collect()

    def _evaluar_condicion(self, pin_in, op, val):
        """Evalúa si la condición asociada a una regla se cumple."""
        if op == 4:
            return True

        objeto_pin = self.pines_in.get(pin_in)
        if objeto_pin is None:
            return False

        lectura = objeto_pin.read() if isinstance(objeto_pin, machine.ADC) else objeto_pin.value()
        if op == 0: return lectura == val
        if op == 1: return lectura != val
        if op == 2: return lectura > val
        if op == 3: return lectura < val
        return False

    def _aplicar_salida(self, pin_num, accion):
        """Aplica la acción de control sobre el pin de salida."""
        pin = self.pines_out.get(pin_num)
        if not pin:
            return
        if accion == 0:
            pin.value(0)
        elif accion == 1:
            pin.value(1)
        elif accion == 2:
            pin.value(not pin.value())

    def ciclo_evaluacion(self):
        """
        Ciclo de escaneo principal. Debe ejecutarse periódicamente dentro
        del bucle principal (while True) sin bloqueos.
        """
        ahora = time.ticks_ms()
        for r in self.reglas:
            r_id, p_in, op, val, p_out, act, t_type, t_ms = r
            condicion_cumplida = self._evaluar_condicion(p_in, op, val)
            t_inicio, t_activo = self.estado_timers.get(r_id, [ahora, False])

            # Tipo 0: Inmediato (Combinacional)
            if t_type == 0:
                self._aplicar_salida(p_out, act if condicion_cumplida else (0 if act == 1 else 1))

            # Tipo 1: Retardo al encendido (TON)
            elif t_type == 1:
                if condicion_cumplida:
                    if not t_activo:
                        self.estado_timers[r_id] = [ahora, True]
                    elif time.ticks_diff(ahora, t_inicio) >= t_ms:
                        self._aplicar_salida(p_out, act)
                else:
                    self.estado_timers[r_id] = [0, False]
                    self._aplicar_salida(p_out, 0)

            # Tipo 2: Pulso Monoestable (PULSE)
            elif t_type == 2:
                if condicion_cumplida and not t_activo:
                    self._aplicar_salida(p_out, act)
                    self.estado_timers[r_id] = [ahora, True]
                elif t_activo:
                    if time.ticks_diff(ahora, t_inicio) >= t_ms:
                        self._aplicar_salida(p_out, 0)
                        if not condicion_cumplida:  # Rearme al cesar condición
                            self.estado_timers[r_id] = [0, False]

            # Tipo 3: Oscilador Periódico Simétrico (BLINK)
            elif t_type == 3:
                if condicion_cumplida:
                    if time.ticks_diff(ahora, t_inicio) >= t_ms:
                        nuevo_estado = not t_activo
                        self.estado_timers[r_id] = [ahora, nuevo_estado]
                        self._aplicar_salida(p_out, 1 if nuevo_estado else 0)
                else:
                    self.estado_timers[r_id] = [ahora, False]
                    self._aplicar_salida(p_out, 0)

            # Tipo 4: Latido de Vida Industrial (HEARTBEAT)
            elif t_type == 4:
                if condicion_cumplida:
                    diferencia = time.ticks_diff(ahora, t_inicio)
                    if diferencia >= t_ms:
                        t_inicio = ahora
                        self.estado_timers[r_id][0] = ahora
                        diferencia = 0

                    # Dos destellos de 80ms separados por 100ms
                    if (0 <= diferencia < 80) or (180 <= diferencia < 260):
                        self._aplicar_salida(p_out, 1)
                    else:
                        self._aplicar_salida(p_out, 0)
                else:
                    self._aplicar_salida(p_out, 0)
                    self.estado_timers[r_id] = [ahora, False]
