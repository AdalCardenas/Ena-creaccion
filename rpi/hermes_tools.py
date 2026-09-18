"""
hermes_tools.py - Herramientas de control y emparejamiento para Hermes Agent.
Proyecto: Ena-creaccion (Capa Raspberry Pi)

Este módulo expone las funciones nativas y esquemas de llamada a herramientas
(Tool/Function Calling) que Hermes Agent utiliza para gobernar los nodos ESP32
a partir de comandos en lenguaje natural.
"""
import json
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

BASE_URL = "http://127.0.0.1:8765"


def _peticion_http(metodo: str, endpoint: str, payload: dict = None) -> dict:
    """Envía peticiones HTTP al demonio local ena_service."""
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, method=metodo)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        return {"error": f"No se pudo contactar al demonio Ena ({e}). ¿Está corriendo ena_service.py?"}


# ============================================================================
# FUNCIONES EJECUTABLES POR HERMES AGENT
# ============================================================================

def listar_dispositivos_nuevos() -> str:
    """
    Lista los microcontroladores ESP32 que se han encendido recientemente
    y están esperando ser configurados y bautizados con un nombre.
    """
    res = _peticion_http("GET", "/api/pending")
    if "error" in res:
        return res["error"]
    pendientes = res.get("nodos_pendientes", [])
    if not pendientes:
        return "No hay ningún nuevo dispositivo pendiente de emparejar en este momento."

    lineas = ["Se han detectado los siguientes dispositivos nuevos:"]
    for p in pendientes:
        lineas.append(f"- Dirección MAC: {p['mac']} (Estado: {p.get('status', 'nuevo')})")
    return "\n".join(lineas)


def identificar_dispositivo(alias_o_mac: str, duracion_segundos: int = 3) -> str:
    """
    Hace parpadear la luz LED azul de un ESP32 durante unos segundos
    para que la persona pueda identificar físicamente cuál dispositivo tiene en la mano.
    """
    res = _peticion_http("POST", "/api/identify", {
        "target": alias_o_mac,
        "duracion_ms": duracion_segundos * 1000
    })
    if "error" in res:
        return res["error"]
    return f"Haciendo parpadear la luz del dispositivo '{alias_o_mac}' durante {duracion_segundos} segundos."


def registrar_dispositivo(alias: str, mac: str, funcion_dispositivo: str,
                          pin_sensor: Optional[int] = None,
                          pin_actuador: Optional[int] = None) -> str:
    """
    Bautiza un nuevo ESP32 con un nombre amigable (ej: 'Luz del lavaloza', 'Aspersor del jardín')
    y guarda los pines donde el usuario conectó sus aparatos o sensores.
    """
    pines = {}
    if pin_actuador is not None:
        pines["actuador"] = {"pin": pin_actuador, "tipo": "digital_out"}
    if pin_sensor is not None:
        pines["sensor"] = {"pin": pin_sensor, "tipo": "digital_in"}

    payload = {
        "alias": alias,
        "mac": mac,
        "descripcion": funcion_dispositivo,
        "pines": pines
    }
    res = _peticion_http("POST", "/api/register", payload)
    if "error" in res:
        return res["error"]
    return f"¡Listo! He registrado el dispositivo como '{alias}' ({mac}). Ya puedes pedirme que lo controle."


def activar_salida_temporal(dispositivo: str, actuador: str, duracion_segundos: float) -> str:
    """
    Ejecuta una acción inmediata por tiempo en un aparato.
    Ejemplo: 'riegue las plantas del jardín por 5 minutos' -> duracion_segundos=300.
    """
    payload = {
        "target": dispositivo,
        "actuador": actuador,
        "segundos": duracion_segundos
    }
    res = _peticion_http("POST", "/api/quick_action", payload)
    if "error" in res:
        return res["error"]

    minutos = duracion_segundos / 60
    tiempo_str = f"{minutos:.1f} minutos" if minutos >= 1 else f"{int(duracion_segundos)} segundos"
    return f"He activado '{actuador}' en '{dispositivo}' durante {tiempo_str}."


def crear_automatizacion(dispositivo: str, pin_sensor: int, condicion: str,
                         pin_actuador: int, accion: str = "encender",
                         tipo_temporizador: str = "inmediato",
                         tiempo_segundos: float = 0) -> str:
    """
    Programa una lógica permanente en el micro-PLC para que funcione de forma autónoma.
    Ejemplo: 'enciende la luz cuando el sensor del pin 4 detecte presencia'.
    """
    from rule_compiler import RuleCompiler

    act_val = 1 if accion.lower() in ["encender", "activar", "high", "1"] else 0
    regla = RuleCompiler.compilar_regla_condicional(
        p_in=pin_sensor,
        operador=condicion,
        valor=1,
        p_out=pin_actuador,
        accion=act_val,
        tipo_timer=tipo_temporizador,
        tiempo_segundos=tiempo_segundos
    )

    payload = {
        "target": dispositivo,
        "reglas": [regla]
    }
    res = _peticion_http("POST", "/api/send_rules", payload)
    if "error" in res:
        return res["error"]
    return f"Automatización guardada y programada con éxito en '{dispositivo}'. El nodo operará de forma autónoma."


def consultar_dispositivos() -> str:
    """Retorna la lista de todos los dispositivos registrados en la casa y sus funciones."""
    res = _peticion_http("GET", "/api/devices")
    if "error" in res:
        return res["error"]
    dispositivos = res.get("dispositivos", [])
    if not dispositivos:
        return "Aún no tienes ningún dispositivo registrado."

    lineas = ["Dispositivos activos en el sistema:"]
    for d in dispositivos:
        pines_str = ", ".join([f"{k}: pin {v if isinstance(v, int) else v.get('pin')}" for k, v in d.get("pines", {}).items()])
        lineas.append(f"- **{d['alias']}** ({d['mac']}): {d.get('descripcion', '')} [Pines: {pines_str}]")
    return "\n".join(lineas)


def borrar_reglas(dispositivo: str) -> str:
    """
    Borra todas las reglas y automatizaciones de un dispositivo,
    apagando sus salidas y dejándolo listo para nueva lógica.
    """
    res = _peticion_http("POST", "/api/clear_rules", {"target": dispositivo})
    if "error" in res:
        return res["error"]
    return f"Se han eliminado todas las reglas de '{dispositivo}'. Sus salidas están apagadas."


def reconfigurar_dispositivo(dispositivo: str, nuevo_nombre: Optional[str] = None,
                             nueva_funcion: Optional[str] = None,
                             pin_sensor: Optional[int] = None,
                             pin_actuador: Optional[int] = None) -> str:
    """
    Modifica la configuración de un dispositivo ya existente, cambiando su nombre,
    función o los pines conectados a él para reprogramarlo o reutilizarlo.
    """
    nuevos_pines = None
    if pin_sensor is not None or pin_actuador is not None:
        nuevos_pines = {}
        if pin_actuador is not None:
            nuevos_pines["actuador"] = {"pin": pin_actuador, "tipo": "digital_out"}
        if pin_sensor is not None:
            nuevos_pines["sensor"] = {"pin": pin_sensor, "tipo": "digital_in"}

    payload = {
        "target": dispositivo,
        "nuevo_alias": nuevo_nombre,
        "nueva_descripcion": nueva_funcion,
        "nuevos_pines": nuevos_pines
    }
    res = _peticion_http("POST", "/api/reconfigure", payload)
    if "error" in res:
        return res["error"]
    d = res.get("dispositivo", {})
    return f"Dispositivo actualizado: ahora es '{d.get('alias', dispositivo)}' ({d.get('descripcion', '')})."


def desvincular_dispositivo(dispositivo: str) -> str:
    """
    Elimina por completo un dispositivo del sistema y borra sus reglas
    en el microcontrolador físico para que vuelva a detectarse como un nodo nuevo.
    """
    res = _peticion_http("POST", "/api/delete_device", {"target": dispositivo})
    if "error" in res:
        return res["error"]
    d = res.get("dispositivo", {})
    alias = d.get("alias", dispositivo)
    return f"El dispositivo '{alias}' ha sido desvinculado y reseteado. Si sigue encendido, volverá a aparecer como nuevo."


def crear_automatizacion_compuesta(dispositivo: str, lista_condiciones: list,
                                   logica: str = "AND", pin_actuador: int = None,
                                   accion: str = "encender",
                                   tipo_temporizador: str = "inmediato",
                                   tiempo_segundos: float = 0) -> str:
    """
    Programa una regla con múltiples condiciones simultáneas (AND/OR).
    Ejemplo: 'enciende el aire si temp > 28 y ventana == cerrada y puerta == cerrada'.
    lista_condiciones es una lista de diccionarios, ej:
    [{"pin": 4, "sub": "temp", "op": ">", "val": 28}, {"pin": 13, "op": "==", "val": 1}]
    """
    from rule_compiler import RuleCompiler

    act_val = 1 if accion.lower() in ["encender", "activar", "high", "1"] else 0
    regla = RuleCompiler.compilar_regla_compuesta(
        condiciones=lista_condiciones,
        logica=logica,
        p_out=pin_actuador,
        accion=act_val,
        tipo_timer=tipo_temporizador,
        tiempo_segundos=tiempo_segundos
    )

    payload = {
        "target": dispositivo,
        "reglas": [regla]
    }
    res = _peticion_http("POST", "/api/send_rules", payload)
    if "error" in res:
        return res["error"]
    return f"Automatización multi-condición ({logica}) programada con éxito en '{dispositivo}'."


def mostrar_en_pantalla(dispositivo: str, lineas: list) -> str:
    """
    Muestra hasta 6 líneas de texto en la pantalla OLED física conectada al ESP32.
    Ejemplo: lineas=["Temp: 24 C", "Aire: ON"]
    """
    payload = {
        "target": dispositivo,
        "lineas": lineas
    }
    res = _peticion_http("POST", "/api/display", payload)
    if "error" in res:
        return res["error"]
    return f"Texto enviado a la pantalla de '{dispositivo}'."


# ============================================================================
# ESQUEMAS DE FUNCTION CALLING / TOOL CALLING PARA HERMES AGENT
# ============================================================================

HERMES_TOOLS_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "listar_dispositivos_nuevos",
            "description": "Descubre microcontroladores ESP32 recién encendidos que aún no han sido bautizados.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "identificar_dispositivo",
            "description": "Hace parpadear el LED azul integrado de un ESP32 durante 3 segundos para confirmar físicamente cuál dispositivo tiene el usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alias_o_mac": {"type": "string", "description": "Nombre amigable o dirección MAC del ESP32 a identificar."},
                    "duracion_segundos": {"type": "integer", "description": "Segundos de parpadeo (por defecto 3)."}
                },
                "required": ["alias_o_mac"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_dispositivo",
            "description": "Asigna un nombre en lenguaje humano y los pines conectados a un nuevo dispositivo ESP32.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "description": "Nombre humano amigable, ej: 'Luz del lavaloza' o 'Aspersor del jardín'."},
                    "mac": {"type": "string", "description": "Dirección MAC del dispositivo descubierta en balizas."},
                    "funcion_dispositivo": {"type": "string", "description": "Breve descripción de qué hace el dispositivo en la casa."},
                    "pin_sensor": {"type": "integer", "description": "Número de pin GPIO del sensor (entrada), si tiene."},
                    "pin_actuador": {"type": "integer", "description": "Número de pin GPIO del actuador o relé (salida), si tiene."}
                },
                "required": ["alias", "mac", "funcion_dispositivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activar_salida_temporal",
            "description": "Ejecuta una acción inmediata con duración de tiempo en un actuador (ej. regar las plantas 5 minutos, encender ventilador 10 minutos).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre amigable del dispositivo (ej. 'aspersor del jardín')."},
                    "actuador": {"type": "string", "description": "Nombre del actuador o relé (ej. 'valvula', 'foco')."},
                    "duracion_segundos": {"type": "number", "description": "Duración en segundos de la acción (ej. 300 para 5 minutos)."}
                },
                "required": ["dispositivo", "actuador", "duracion_segundos"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crear_automatizacion",
            "description": "Programa una regla autónoma permanente en el micro-PLC para que responda a sensores sin requerir la Raspberry Pi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre del dispositivo donde se instalará la regla."},
                    "pin_sensor": {"type": "integer", "description": "Pin de entrada del sensor."},
                    "condicion": {"type": "string", "description": "Condición lógica: 'detecta', '==', '>', '<'."},
                    "pin_actuador": {"type": "integer", "description": "Pin de salida que se accionará."},
                    "accion": {"type": "string", "description": "'encender' o 'apagar'."},
                    "tipo_temporizador": {"type": "string", "description": "'inmediato', 'retardo' (TON), o 'pulso'."},
                    "tiempo_segundos": {"type": "number", "description": "Segundos para el temporizador, si aplica."}
                },
                "required": ["dispositivo", "pin_sensor", "condicion", "pin_actuador"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_dispositivos",
            "description": "Obtiene la lista de todos los dispositivos registrados en la casa, sus pines y sus funciones.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "borrar_reglas",
            "description": "Borra todas las reglas y automatizaciones de un dispositivo para detener su funcionamiento autónomo y apagar sus salidas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre amigable del dispositivo a limpiar."}
                },
                "required": ["dispositivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reconfigurar_dispositivo",
            "description": "Modifica un dispositivo ya registrado: cambia su nombre, su función o los pines conectados a él.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre actual del dispositivo a modificar."},
                    "nuevo_nombre": {"type": "string", "description": "Nuevo nombre si se desea renombrar."},
                    "nueva_funcion": {"type": "string", "description": "Nueva descripción de la función."},
                    "pin_sensor": {"type": "integer", "description": "Nuevo pin del sensor, si cambió el cableado."},
                    "pin_actuador": {"type": "integer", "description": "Nuevo pin del actuador, si cambió el cableado."}
                },
                "required": ["dispositivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desvincular_dispositivo",
            "description": "Elimina un dispositivo del sistema y resetea su microcontrolador para que pueda ser configurado desde cero.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre del dispositivo a desvincular y resetear."}
                },
                "required": ["dispositivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crear_automatizacion_compuesta",
            "description": "Programa una regla con múltiples condiciones evaluadas con lógica AND o OR (ej. si temp > 28 y ventana cerrada y puerta cerrada entonces prender aire).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre del dispositivo donde se instalará la regla."},
                    "lista_condiciones": {
                        "type": "array",
                        "description": "Lista de condiciones. Cada una con 'pin', 'op' (==, !=, >, <), 'val', y opcional 'sub' ('temp' o 'hum').",
                        "items": {"type": "object"}
                    },
                    "logica": {"type": "string", "enum": ["AND", "OR"], "description": "'AND' (todas deben cumplirse) o 'OR' (al menos una)."},
                    "pin_actuador": {"type": "integer", "description": "Pin de salida que se accionará."},
                    "accion": {"type": "string", "description": "'encender' o 'apagar'."},
                    "tipo_temporizador": {"type": "string", "description": "'inmediato', 'retardo' (TON), o 'pulso'."},
                    "tiempo_segundos": {"type": "number", "description": "Segundos para el temporizador, si aplica."}
                },
                "required": ["dispositivo", "lista_condiciones", "pin_actuador"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mostrar_en_pantalla",
            "description": "Envía hasta 6 líneas de texto para mostrar en la pantalla OLED del ESP32.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {"type": "string", "description": "Nombre del dispositivo con pantalla."},
                    "lineas": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de hasta 6 textos a mostrar en el display."
                    }
                },
                "required": ["dispositivo", "lineas"]
            }
        }
    }
]

