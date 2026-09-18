"""
rule_compiler.py - Compilador de intenciones de alto nivel a reglas del Micro-PLC.
Proyecto: Ena-creaccion (Capa Raspberry Pi)
"""
import json

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

MAPA_OPERADORES = {
    "==": OP_IGUAL,
    "=": OP_IGUAL,
    "igual": OP_IGUAL,
    "detecta": OP_IGUAL,
    "!=": OP_DIFERENTE,
    "diferente": OP_DIFERENTE,
    "no_detecta": OP_DIFERENTE,
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
    "heartbeat": TIMER_HEARTBEAT
}


class RuleCompiler:
    """Compila configuraciones lógicas legibles a la matriz compacta de 8 elementos."""

    @staticmethod
    def compilar_regla_condicional(p_in: int, operador, valor: int, p_out: int,
                                   accion: int = 1, tipo_timer="inmediato",
                                   tiempo_segundos: float = 0, r_id: int = 1):
        """
        Crea una regla condicional basada en sensor.
        Retorna: [r_id, p_in, op, val, p_out, act, t_type, t_ms]
        """
        if isinstance(operador, str):
            op_code = MAPA_OPERADORES.get(operador.lower(), OP_IGUAL)
        else:
            op_code = int(operador)

        if isinstance(tipo_timer, str):
            t_type_code = MAPA_TIMERS.get(tipo_timer.lower(), TIMER_INMEDIATO)
        else:
            t_type_code = int(tipo_timer)

        t_ms = int(tiempo_segundos * 1000)

        return [r_id, p_in, op_code, valor, p_out, accion, t_type_code, t_ms]

    @staticmethod
    def compilar_accion_temporizada(p_out: int, duracion_segundos: float,
                                    accion: int = 1, r_id: int = 99):
        """
        Crea una acción inmediata por tiempo (ej. regar por 5 minutos).
        Usa condición incondicional (op=4) y temporizador monoestable (t_type=2).
        """
        t_ms = int(duracion_segundos * 1000)
        return [r_id, -1, OP_INCONDICIONAL, 0, p_out, accion, TIMER_PULSO, t_ms]

    @staticmethod
    def compilar_regla_compuesta(condiciones: list, logica: str = "AND",
                                  p_out: int = None, accion: int = 1,
                                  tipo_timer="inmediato", tiempo_segundos: float = 0,
                                  r_id: int = 1):
        """
        Crea una regla con múltiples condiciones evaluadas con lógica AND o OR.
        Cada condición en 'condiciones' es:
        - {"pin": 4, "op": ">", "val": 28, "sub": "temp"} o [pin, op, val]
        Retorna: [r_id, [[p1, op1, v1], [p2, op2, v2], ...], logica_code, 0, p_out, act, t_type, t_ms]
        """
        subcondiciones = []
        for c in condiciones:
            if isinstance(c, dict):
                p = c.get("pin")
                sub_canal = c.get("sub")
                p_spec = [p, sub_canal] if sub_canal else p
                op = c.get("op", "==")
                op_code = MAPA_OPERADORES.get(str(op).lower(), OP_IGUAL)
                val = c.get("val", 1)
                subcondiciones.append([p_spec, op_code, val])
            elif isinstance(c, (list, tuple)):
                p = c[0]
                op = c[1]
                op_code = MAPA_OPERADORES.get(str(op).lower(), op) if isinstance(op, str) else int(op)
                val = c[2]
                subcondiciones.append([p, op_code, val])

        logica_code = 0 if str(logica).upper() == "AND" else 1

        if isinstance(tipo_timer, str):
            t_type_code = MAPA_TIMERS.get(tipo_timer.lower(), TIMER_INMEDIATO)
        else:
            t_type_code = int(tipo_timer)

        t_ms = int(tiempo_segundos * 1000)

        return [r_id, subcondiciones, logica_code, 0, p_out, accion, t_type_code, t_ms]

    @staticmethod
    def empaquetar_reglas(lista_reglas):
        """
        Envuelve la lista de reglas en el formato de trama esperado por el nodo:
        {"r": [[...], [...]]}
        Verifica que no exceda el límite físico de 250 bytes de ESP-NOW.
        """
        trama = {"r": lista_reglas}
        serializado = json.dumps(trama, separators=(',', ':')).encode('utf-8')
        tamano = len(serializado)

        if tamano > 250:
            raise ValueError(f"La trama ({tamano} bytes) supera el límite máximo de 250 bytes de ESP-NOW.")

        return trama, tamano
