"""
rule_compiler.py - Compilador de intenciones de alto nivel a reglas del Micro-PLC.
Proyecto: Ena-creaccion (Capa Raspberry Pi / ESP8266 ESP-01S)
"""
import json

# Identificador lógico del ADC del ESP8266 (no es un GPIO físico).
GPIO_ADC = 17

# Constantes de Operadores del Micro-PLC
OP_IGUAL = 0
OP_DIFERENTE = 1
OP_MAYOR = 2
OP_MENOR = 3
OP_INCONDICIONAL = 4

# Constantes de Tipos de Temporizadores
TIMER_INMEDIATO = 0
TIMER_TON = 1
TIMER_PULSO = 2
TIMER_BLINK = 3
TIMER_HEARTBEAT = 4
TIMER_FADE = 5

MAPA_OPERADORES = {
    "==": OP_IGUAL,
    "=": OP_IGUAL,
    "igual": OP_IGUAL,
    "detecta": OP_IGUAL,
    "presionado": OP_IGUAL,
    "!=": OP_DIFERENTE,
    "diferente": OP_DIFERENTE,
    "no_detecta": OP_DIFERENTE,
    "suelto": OP_DIFERENTE,
    ">": OP_MAYOR,
    "mayor": OP_MAYOR,
    "<": OP_MENOR,
    "menor": OP_MENOR,
    "siempre": OP_INCONDICIONAL,
    "incondicional": OP_INCONDICIONAL
}

MAPA_TIMERS = {
    "inmediato": TIMER_INMEDIATO,
    "directo": TIMER_INMEDIATO,
    "ton": TIMER_TON,
    "retardo": TIMER_TON,
    "pulso": TIMER_PULSO,
    "monoestable": TIMER_PULSO,
    "blink": TIMER_BLINK,
    "parpadeo": TIMER_BLINK,
    "heartbeat": TIMER_HEARTBEAT,
    "fade": TIMER_FADE,
    "pwm": TIMER_FADE,
    "desvanecimiento": TIMER_FADE,
    "respiracion": TIMER_FADE
}

MAPA_PINES_ESP01S = {
    # Pines físicos directos del ESP-01S (8 pines)
    "IO0": 0, "GPIO0": 0, "0": 0, "BOTON1": 0, "D3": 0,
    "IO1": 1, "GPIO1": 1, "1": 1, "TX": 1, "TXD": 1,
    "IO2": 2, "GPIO2": 2, "2": 2, "LED": 2, "LED_AZUL": 2, "D4": 2,
    "IO3": 3, "GPIO3": 3, "3": 3, "RX": 3, "RXD": 3, "BOTON2": 3,

    # Mapeo de compatibilidad con NodeMCU / D1 Mini
    "D0": 16, "D1": 5, "D2": 4, "D5": 14, "D6": 12, "D7": 13, "D8": 15,

    # Entrada analógica única del ESP8266 (canal 0, expuesta como GPIO 17)
    "A0": GPIO_ADC, "ADC": GPIO_ADC, "AD": GPIO_ADC, "ANALOG": GPIO_ADC,
}

# El gateway acepta 250 bytes de ESP-NOW; la trama firmada se queda en 240.
MAX_TRAMA = 240


class RuleCompiler:
    """Compila configuraciones lógicas legibles a la matriz compacta de 8 elementos."""

    @staticmethod
    def normalizar_pin(pin):
        """Traduce alias legibles ('IO0', 'RX', 'IO2', 'LED') a enteros GPIO."""
        if pin is None:
            return -1
        if isinstance(pin, int):
            return pin
        clave = str(pin).strip().upper()
        if clave in MAPA_PINES_ESP01S:
            return MAPA_PINES_ESP01S[clave]
        try:
            return int(clave)
        except ValueError:
            return pin

    @classmethod
    def compilar_regla_condicional(cls, p_in, operador, valor: int, p_out,
                                   accion: int = 1, tipo_timer="inmediato",
                                   tiempo_segundos: float = 0, r_id: int = 1):
        """
        Crea una regla condicional basada en sensor o botón.
        Retorna: [r_id, p_in, op, val, p_out, act, t_type, t_ms]
        """
        pin_in_num = cls.normalizar_pin(p_in)
        pin_out_num = cls.normalizar_pin(p_out)

        if isinstance(operador, str):
            op_code = MAPA_OPERADORES.get(operador.lower(), OP_IGUAL)
        else:
            op_code = int(operador)

        if isinstance(tipo_timer, str):
            t_type_code = MAPA_TIMERS.get(tipo_timer.lower(), TIMER_INMEDIATO)
        else:
            t_type_code = int(tipo_timer)

        t_ms = int(tiempo_segundos * 1000)

        return [r_id, pin_in_num, op_code, valor, pin_out_num, accion, t_type_code, t_ms]

    @classmethod
    def compilar_accion_temporizada(cls, p_out, duracion_segundos: float,
                                    accion: int = 1, r_id: int = 99):
        """
        Crea una acción inmediata por tiempo (ej. regar o encender por 5 minutos).
        Usa condición incondicional (op=4) y temporizador monoestable (t_type=2).
        """
        pin_out_num = cls.normalizar_pin(p_out)
        t_ms = int(duracion_segundos * 1000)
        return [r_id, -1, OP_INCONDICIONAL, 0, pin_out_num, accion, TIMER_PULSO, t_ms]

    @classmethod
    def compilar_regla_compuesta(cls, condiciones: list, logica: str = "AND",
                                  p_out=None, accion: int = 1,
                                  tipo_timer="inmediato", tiempo_segundos: float = 0,
                                  r_id: int = 1):
        """
        Crea una regla con múltiples condiciones evaluadas con lógica AND o OR.
        Cada condición en 'condiciones' es:
        - {"pin": "IO0", "op": "==", "val": 0, "sub": "temp"} o [pin, op, val]
        Retorna: [r_id, [[p1, op1, v1], [p2, op2, v2], ...], logica_code, 0, p_out, act, t_type, t_ms]
        """
        subcondiciones = []
        for c in condiciones:
            if isinstance(c, dict):
                p_raw = c.get("pin")
                p_norm = cls.normalizar_pin(p_raw)
                sub_canal = c.get("sub")
                p_spec = [p_norm, sub_canal] if sub_canal else p_norm
                op = c.get("op", "==")
                op_code = MAPA_OPERADORES.get(str(op).lower(), OP_IGUAL)
                val = c.get("val", 1)
                subcondiciones.append([p_spec, op_code, val])
            elif isinstance(c, (list, tuple)):
                p_norm = cls.normalizar_pin(c[0])
                op = c[1]
                op_code = MAPA_OPERADORES.get(str(op).lower(), op) if isinstance(op, str) else int(op)
                val = c[2]
                subcondiciones.append([p_norm, op_code, val])

        pin_out_num = cls.normalizar_pin(p_out)
        logica_code = 0 if str(logica).upper() == "AND" else 1

        if isinstance(tipo_timer, str):
            t_type_code = MAPA_TIMERS.get(tipo_timer.lower(), TIMER_INMEDIATO)
        else:
            t_type_code = int(tipo_timer)

        t_ms = int(tiempo_segundos * 1000)

        return [r_id, subcondiciones, logica_code, 0, pin_out_num, accion, t_type_code, t_ms]

    @staticmethod
    def empaquetar_reglas(lista_reglas, hw=None):
        """
        Envuelve la lista de reglas en el formato de trama esperado por el nodo:
        {"r": [[...], [...]], "hw": [{"pin": 4, "tipo": "dht22"}]}

        Se usa para configuraciones que caben en una sola trama. Para listas
        mayores, `protocolo.planificar()` las reparte en lotes con confirmación.

        Verifica que no exceda el límite físico de ESP-NOW (250 bytes).
        """
        trama = {"r": lista_reglas}
        if hw:
            trama["hw"] = hw

        serializado = json.dumps(trama, separators=(',', ':')).encode('utf-8')
        tamano = len(serializado)

        if tamano > MAX_TRAMA:
            raise ValueError(
                f"La trama ({tamano} bytes) supera el límite de {MAX_TRAMA} "
                f"bytes de ESP-NOW; usa varias reglas más pequeñas o el envío "
                f"por lotes."
            )

        return trama, tamano
