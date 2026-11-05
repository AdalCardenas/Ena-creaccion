"""
motor_reglas.py - Motor de Reglas reactivo, no bloqueante y multi-condición para Micro-PLC.

Proyecto: Ena-creaccion / Ena-receptor (ESP8266 / ESP-01S)

Formato de regla (8 elementos):

    [r_id, cond, op_logica, val, p_out, act, t_type, t_ms]

- Regla simple:     cond = pin de entrada, op_logica = operador (0..4), val = umbral.
- Regla compuesta:  cond = [[pin, op, val], ...], op_logica = 0 (AND) / 1 (OR).

El tipo de cada pin ya no se adivina por el umbral (`val > 1`): lo declara el
host en la lista `hw` de la trama. Sin declaración, el pin se trata como
entrada digital. Un sensor analógico comparado contra 0/1 antes se configuraba
por error como digital y jamás devolvía lecturas.
"""
try:
    import ujson as ujson
except ImportError:  # CPython
    import json as ujson

import gc

try:
    from . import protocolo
    from . import reloj
    from .hal import HardwareManager, resolver_pin
except ImportError:  # ejecución plana en el dispositivo
    import protocolo
    import reloj
    from hal import HardwareManager, resolver_pin

# Tipos declarados en la lista "hw" de la trama.
TIPOS_ADC = ("adc", "analog", "analogico", "analógica")
TIPOS_ENTRADA = ("digital", "digital_in", "entrada", "boton", "button", "pulsador")
TIPOS_SALIDA = ("digital_out", "salida", "rele", "relay", "led", "pwm")
TIPOS_DHT = ("dht22", "dht11", "dht")
TIPOS_OLED = ("oled", "ssd1306")

# Frecuencia mínima de los efectos visuales: evita conmutar a 100 Hz.
MIN_MS_EFECTO = 20


class MotorReglas:
    """Ejecución de reglas con soporte para AND/OR y seis temporizadores."""

    def __init__(self, hal: HardwareManager = None):
        self.hal = hal if hal is not None else HardwareManager()
        self.reglas = []
        self.estado_timers = {}
        self.pines_adc = set()
        self._errores_ciclo = 0

    # =====================================================================
    # CARGA Y VALIDACIÓN
    # =====================================================================

    def aplicar_reglas(self, reglas, hw=None):
        """
        Valida, inicializa el hardware declarado y reemplaza la configuración.

        Lanza protocolo.ErrorProtocolo si algo es inválido, de modo que el nodo
        pueda responder con un error en vez de aplicar media configuración.
        """
        limpias, motivo = protocolo.validar_reglas(reglas)
        if motivo:
            raise protocolo.ErrorProtocolo(motivo)

        descriptores, motivo_hw = protocolo.validar_hw(hw)
        if motivo_hw:
            raise protocolo.ErrorProtocolo(motivo_hw)

        self.reglas = limpias
        self.estado_timers = {}
        self.pines_adc = set()

        for item in descriptores:
            self._configurar_hw(item)
        for indice in range(len(self.reglas)):
            self._preparar_regla(indice)

        gc.collect()
        return len(self.reglas)

    def cargar_reglas(self, trama_bytes):
        """
        Compatibilidad: acepta la trama completa {"r": [...], "hw": [...]}.

        Retorna True si la configuración se aplicó.
        """
        try:
            datos = ujson.loads(trama_bytes)
        except (ValueError, OSError) as err:
            print(f"[Motor] Trama ilegible: {err}")
            return False

        if not isinstance(datos, dict):
            print("[Motor] La trama no es un objeto JSON.")
            return False

        try:
            self.aplicar_reglas(datos.get("r", []), datos.get("hw"))
            return True
        except protocolo.ErrorProtocolo as err:
            print(f"[Motor] Configuracion rechazada: {err}")
            return False

    def _configurar_hw(self, item):
        """Inicializa un descriptor de hardware declarado por el host."""
        tipo = str(item.get("tipo", "")).strip().lower()
        pin = resolver_pin(item.get("pin"))

        if tipo in TIPOS_ADC:
            if pin is None:
                return
            self.pines_adc.add(pin)
            self.hal.asegurar_pin_entrada(pin, es_adc=True)
            return

        if tipo in TIPOS_DHT:
            self.hal.asegurar_dht(pin, es_dht22=(tipo != "dht11"))
            return

        if tipo in TIPOS_OLED:
            self.hal.asegurar_oled(
                sda_pin=item.get("sda", 0),
                scl_pin=item.get("scl", 2),
                ancho=item.get("ancho", 128),
                alto=item.get("alto", 64),
            )
            return

        if tipo in TIPOS_SALIDA:
            self.hal.asegurar_pin_salida(pin)
            return

        # Entrada digital: es el valor por omisión.
        self.hal.asegurar_pin_entrada(pin, es_adc=False)

    def _preparar_regla(self, indice):
        regla = self.reglas[indice]
        self.estado_timers[indice] = [reloj.ahora_ms(), False]

        p_out = resolver_pin(regla[4])
        if p_out is not None:
            self.hal.asegurar_pin_salida(p_out)

        for pin in self._pines_de_condicion(regla[1]):
            if pin is None:
                continue
            if pin in self.hal.sensores_dht:
                continue  # el DHT gestiona su propio objeto de pin
            self.hal.asegurar_pin_entrada(pin, es_adc=(pin in self.pines_adc))

    def _pines_de_condicion(self, cond):
        """Extrae los GPIO implicados en una condición simple o compuesta."""
        resultado = []
        if isinstance(cond, (list, tuple)) and len(cond) > 0 \
                and isinstance(cond[0], (list, tuple)):
            for sub in cond:
                espec = sub[0]
                if isinstance(espec, (list, tuple)):
                    resultado.append(resolver_pin(espec[0]))
                else:
                    resultado.append(resolver_pin(espec))
        else:
            resultado.append(resolver_pin(cond))
        return resultado

    def apagar_todo(self):
        """Apaga todas las salidas y descarta los temporizadores en curso."""
        self.hal.apagar_todas_las_salidas()
        self.estado_timers = {}
        for indice in range(len(self.reglas)):
            self.estado_timers[indice] = [reloj.ahora_ms(), False]

    # =====================================================================
    # EVALUACIÓN DE CONDICIONES
    # =====================================================================

    def _evaluar_condicion_simple(self, p_spec, op, val):
        if op == 4:  # incondicional
            return True

        if isinstance(p_spec, (list, tuple)):
            pin_num = resolver_pin(p_spec[0])
            sub_canal = p_spec[1] if len(p_spec) > 1 else None
        else:
            pin_num = resolver_pin(p_spec)
            sub_canal = None

        if pin_num is None or pin_num < 0:
            return False

        lectura = self.hal.leer_sensor(pin_num, sub_canal)
        try:
            if op == 0:
                return lectura == val
            if op == 1:
                return lectura != val
            if op == 2:
                return lectura > val
            if op == 3:
                return lectura < val
        except TypeError:
            return False
        return False

    def _evaluar_condicion(self, cond_spec, op_o_logica, val):
        """Evalúa una condición simple o una conjunción/disyunción."""
        if isinstance(cond_spec, (list, tuple)) and len(cond_spec) > 0 \
                and isinstance(cond_spec[0], (list, tuple)):
            es_and = op_o_logica in (0, "AND", "and")
            if es_and:
                for sub in cond_spec:
                    if not self._evaluar_condicion_simple(sub[0], sub[1], sub[2]):
                        return False
                return True
            for sub in cond_spec:
                if self._evaluar_condicion_simple(sub[0], sub[1], sub[2]):
                    return True
            return False

        return self._evaluar_condicion_simple(cond_spec, op_o_logica, val)

    # =====================================================================
    # CICLO DE ESCANEO (~100 Hz)
    # =====================================================================

    def ciclo_evaluacion(self):
        """Ciclo principal del micro-PLC, sin bloqueos."""
        ahora = reloj.ahora_ms()
        for indice in range(len(self.reglas)):
            try:
                self._evaluar_regla(indice, ahora)
            except Exception as err:
                # Una regla defectuosa no debe tumbar el nodo ni inundar la consola.
                self._errores_ciclo += 1
                if self._errores_ciclo <= 5:
                    print(f"[Motor] Error en regla {indice}: {err}")

    @staticmethod
    def _complemento(act):
        if act == 1:
            return 0
        if act == 0:
            return 1
        return 0

    def _evaluar_regla(self, indice, ahora):
        regla = self.reglas[indice]
        cond, op_logica, val, p_out, act, t_type, t_ms = (
            regla[1], regla[2], regla[3], regla[4], regla[5], regla[6], regla[7]
        )
        pin_out = resolver_pin(p_out)
        cumple = self._evaluar_condicion(cond, op_logica, val)

        estado = self.estado_timers.get(indice)
        if estado is None:
            estado = [ahora, False]
            self.estado_timers[indice] = estado
        t_inicio, t_activo = estado

        # Tipo 0: inmediato / combinacional.
        if t_type == 0:
            if act == 2:
                # Conmutar sólo en el flanco, no en cada ciclo de 10 ms.
                if cumple and not t_activo:
                    self.hal.aplicar_salida(pin_out, 2)
                self.estado_timers[indice] = [t_inicio, cumple]
            else:
                self.hal.aplicar_salida(
                    pin_out, act if cumple else self._complemento(act)
                )

        # Tipo 1: retardo al encendido (TON).
        elif t_type == 1:
            if cumple:
                if not t_activo:
                    self.estado_timers[indice] = [ahora, True]
                elif reloj.diferencia(ahora, t_inicio) >= t_ms:
                    self.hal.aplicar_salida(pin_out, act)
            else:
                self.estado_timers[indice] = [ahora, False]
                self.hal.aplicar_salida(pin_out, self._complemento(act))

        # Tipo 2: pulso monoestable.
        elif t_type == 2:
            if cumple and not t_activo:
                self.hal.aplicar_salida(pin_out, act)
                self.estado_timers[indice] = [ahora, True]
            elif t_activo:
                if reloj.diferencia(ahora, t_inicio) >= t_ms:
                    self.hal.aplicar_salida(pin_out, self._complemento(act))
                    if not cumple:
                        self.estado_timers[indice] = [ahora, False]
            else:
                self.hal.aplicar_salida(pin_out, self._complemento(act))

        # Tipo 3: oscilador periódico simétrico (BLINK).
        elif t_type == 3:
            periodo = t_ms if t_ms >= MIN_MS_EFECTO else MIN_MS_EFECTO
            if cumple:
                if reloj.diferencia(ahora, t_inicio) >= periodo:
                    nuevo = not t_activo
                    self.estado_timers[indice] = [ahora, nuevo]
                    self.hal.aplicar_salida(pin_out, 1 if nuevo else 0)
            else:
                self.estado_timers[indice] = [ahora, False]
                self.hal.aplicar_salida(pin_out, 0)

        # Tipo 4: latido de vida industrial (doble destello).
        elif t_type == 4:
            periodo = t_ms if t_ms >= 500 else 500
            if cumple:
                fase = reloj.diferencia(ahora, t_inicio) % periodo
                encendido = (fase < 80) or (180 <= fase < 260)
                self.hal.aplicar_salida(pin_out, 1 if encendido else 0)
            else:
                self.estado_timers[indice] = [ahora, False]
                self.hal.aplicar_salida(pin_out, 0)

        # Tipo 5: desvanecimiento PWM / respiración.
        elif t_type == 5:
            medio = t_ms if t_ms >= MIN_MS_EFECTO else MIN_MS_EFECTO
            if cumple:
                ciclo = 2 * medio
                fase = reloj.diferencia(ahora, t_inicio) % ciclo
                if fase < medio:
                    brillo = fase / medio
                else:
                    brillo = (ciclo - fase) / medio
                self.hal.aplicar_pwm(pin_out, brillo)
            else:
                self.hal.aplicar_pwm(pin_out, 0.0)
                self.estado_timers[indice] = [ahora, False]
