"""
hermes_tools.py - Herramientas de control y emparejamiento para Hermes Agent.

Proyecto: Ena-creaccion (Capa Raspberry Pi)

Este módulo expone las funciones nativas y esquemas de llamada a herramientas
(Tool/Function Calling) que Hermes Agent utiliza para gobernar los nodos
receptores a partir de comandos en lenguaje natural.

El token de la API se lee de `ena_config.json` (o `ENA_TOKEN`), así que el
agente nunca tiene que manejarlo. Cada petición viaja autenticada al demonio
local.
"""
import json
import urllib.error
import urllib.request
from typing import Optional, Union

import config

BASE_URL = "http://127.0.0.1:8765"


def _peticion_http(metodo: str, endpoint: str, payload: dict = None) -> dict:
    """Envía peticiones HTTP autenticadas al demonio local ena_service."""
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=metodo)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {config.token()}")

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # El demonio responde JSON también en los errores: se propaga el motivo.
        try:
            cuerpo = json.loads(err.read().decode("utf-8"))
            return {"error": cuerpo.get("detalle") or cuerpo.get("error") or str(err)}
        except Exception:
            return {"error": f"HTTP {err.code} al contactar al demonio Ena"}
    except urllib.error.URLError as err:
        return {
            "error": f"No se pudo contactar al demonio Ena ({err}). "
                     f"¿Está corriendo ena_service.py?"
        }
    except Exception as err:
        return {"error": f"Fallo inesperado hablando con ena_service: {err}"}


# ============================================================================
# FUNCIONES EJECUTABLES POR HERMES AGENT
# ============================================================================

def listar_dispositivos_nuevos() -> str:
    """
    Lista los microcontroladores que se han encendido recientemente
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
        estado_auth = p.get("autenticado")
        if estado_auth is True:
            marca = "firma verificada"
        elif estado_auth is False:
            marca = ("SIN FIRMA VÁLIDA: revisa el secreto antes de usarlo")
        else:
            marca = "sin secreto configurado"
        lineas.append(
            f"- Dirección MAC: {p['mac']} (Estado: {p.get('status', 'nuevo')}; "
            f"{marca})"
        )
    return "\n".join(lineas)


def identificar_dispositivo(alias_o_mac: str, duracion_segundos: int = 3) -> str:
    """
    Hace parpadear el LED azul integrado de un dispositivo durante unos segundos
    para que la persona pueda identificar físicamente cuál nodo tiene en la mano.
    """
    res = _peticion_http("POST", "/api/identify", {
        "target": alias_o_mac,
        "duracion_ms": int(duracion_segundos * 1000),
    })
    if "error" in res:
        return res["error"]
    return (
        f"Haciendo parpadear la luz del dispositivo '{alias_o_mac}' "
        f"durante {duracion_segundos} segundos."
    )


def registrar_dispositivo(alias: str, mac: str, funcion_dispositivo: str,
                          pin_sensor: Optional[Union[int, str]] = None,
                          pin_actuador: Optional[Union[int, str]] = None,
                          tipo_sensor: Optional[str] = None,
                          tipo_actuador: Optional[str] = None) -> str:
    """
    Bautiza un nuevo nodo con un nombre amigable (ej: 'Luz del lavaloza') y
    guarda los pines donde el usuario conectó sus aparatos o pulsadores.

    `tipo_sensor` acepta 'digital', 'adc', 'dht11' o 'dht22'; declararlo es lo
    que permite leer un sensor analógico (antes se deducía del umbral y fallaba).
    Los pines admiten 'IO0', 'RX', 'IO2', 'TX', 'A0' o enteros.
    """
    pines = {}
    if pin_actuador is not None:
        pines["actuador"] = {
            "pin": pin_actuador,
            "tipo": tipo_actuador or "digital_out",
        }
    if pin_sensor is not None:
        pines["sensor"] = {
            "pin": pin_sensor,
            "tipo": tipo_sensor or "digital_in",
        }

    payload = {
        "alias": alias,
        "mac": mac,
        "descripcion": funcion_dispositivo,
        "pines": pines,
    }
    res = _peticion_http("POST", "/api/register", payload)
    if "error" in res:
        return res["error"]

    aviso = ""
    if res.get("dispositivo", {}).get("autenticado") is False:
        aviso = ("\nAVISO: el nodo no firmó su baliza; el control quedará "
                 "bloqueado hasta que coincida el secreto compartido.")
    return (
        f"¡Listo! He registrado el dispositivo como '{alias}' ({mac}). "
        f"Ya puedes pedirme que lo controle.{aviso}"
    )


def activar_salida_temporal(dispositivo: str, actuador: str,
                            duracion_segundos: float) -> str:
    """
    Ejecuta una acción inmediata por tiempo en un aparato.
    Ejemplo: 'enciende el LED por 10 segundos' -> duracion_segundos=10.
    """
    payload = {
        "target": dispositivo,
        "actuador": actuador,
        "segundos": duracion_segundos,
    }
    res = _peticion_http("POST", "/api/quick_action", payload)
    if "error" in res:
        return res["error"]

    minutos = duracion_segundos / 60
    tiempo_str = f"{minutos:.1f} minutos" if minutos >= 1 else f"{int(duracion_segundos)} segundos"
    return f"He activado '{actuador}' en '{dispositivo}' durante {tiempo_str}."


def crear_automatizacion(dispositivo: str, pin_sensor: Union[int, str],
                         condicion: str, pin_actuador: Union[int, str],
                         accion: str = "encender",
                         tipo_temporizador: str = "inmediato",
                         tiempo_segundos: float = 0) -> str:
    """
    Programa una lógica permanente en el micro-PLC para que funcione de forma autónoma.
    Soporta pines como 'IO0', 'RX', 'IO2', 'TX', 'A0' o enteros.
    """
    from rule_compiler import RuleCompiler

    act_val = 1 if accion.lower() in ["encender", "activar", "high", "1"] else 0
    regla = RuleCompiler.compilar_regla_condicional(
        p_in=pin_sensor,
        operador=condicion,
        valor=0 if condicion.lower() in ["presionado", "detecta", "pulsado"] and str(pin_sensor).upper() in ["IO0", "RX", "0", "3"] else 1,
        p_out=pin_actuador,
        accion=act_val,
        tipo_timer=tipo_temporizador,
        tiempo_segundos=tiempo_segundos,
    )

    payload = {"target": dispositivo, "reglas": [regla]}
    res = _peticion_http("POST", "/api/send_rules", payload)
    if "error" in res:
        return res["error"]
    return (
        f"Automatización guardada y programada con éxito en '{dispositivo}'. "
        f"El nodo operará de forma autónoma."
    )


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
        pines = d.get("pines", {}) or {}
        pines_str = ", ".join(
            f"{k}: pin {v if isinstance(v, (int, str)) else v.get('pin')}"
            f" ({v.get('tipo', 'digital_in') if isinstance(v, dict) else 'digital_in'})"
            for k, v in pines.items()
        )
        auth = d.get("autenticado")
        estado = "firma OK" if auth else ("SIN FIRMA" if auth is False else "sin secreto")
        lineas.append(
            f"- **{d['alias']}** ({d['mac']}): {d.get('descripcion', '')} "
            f"[Pines: {pines_str}] [{estado}]"
        )
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
                             pin_sensor: Optional[Union[int, str]] = None,
                             pin_actuador: Optional[Union[int, str]] = None,
                             tipo_sensor: Optional[str] = None,
                             tipo_actuador: Optional[str] = None) -> str:
    """
    Modifica la configuración de un dispositivo ya existente, cambiando su nombre,
    función o los pines conectados a él para reprogramarlo o reutilizarlo.
    """
    nuevos_pines = None
    if pin_sensor is not None or pin_actuador is not None:
        nuevos_pines = {}
        if pin_actuador is not None:
            nuevos_pines["actuador"] = {
                "pin": pin_actuador,
                "tipo": tipo_actuador or "digital_out",
            }
        if pin_sensor is not None:
            nuevos_pines["sensor"] = {
                "pin": pin_sensor,
                "tipo": tipo_sensor or "digital_in",
            }

    payload = {
        "target": dispositivo,
        "nuevo_alias": nuevo_nombre,
        "nueva_descripcion": nueva_funcion,
        "nuevos_pines": nuevos_pines,
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
                                   logica: str = "AND",
                                   pin_actuador: Union[int, str] = None,
                                   accion: str = "encender",
                                   tipo_temporizador: str = "inmediato",
                                   tiempo_segundos: float = 0) -> str:
    """
    Programa una regla con múltiples condiciones simultáneas (AND/OR).
    Ejemplo: 'enciende el LED si boton1 presionado y boton2 suelto'.
    lista_condiciones: [{"pin": "IO0", "op": "==", "val": 0}, {"pin": "RX", "op": "==", "val": 1}]
    """
    from rule_compiler import RuleCompiler

    act_val = 1 if accion.lower() in ["encender", "activar", "high", "1"] else 0
    regla = RuleCompiler.compilar_regla_compuesta(
        condiciones=lista_condiciones,
        logica=logica,
        p_out=pin_actuador,
        accion=act_val,
        tipo_timer=tipo_temporizador,
        tiempo_segundos=tiempo_segundos,
    )

    payload = {"target": dispositivo, "reglas": [regla]}
    res = _peticion_http("POST", "/api/send_rules", payload)
    if "error" in res:
        return res["error"]
    return f"Automatización multi-condición ({logica}) programada con éxito en '{dispositivo}'."


def mostrar_en_pantalla(dispositivo: str, lineas: list) -> str:
    """
    Muestra hasta 6 líneas de texto en la pantalla OLED conectada al receptor.
    Ejemplo: lineas=["Temp: 24 C", "Aire: ON"]
    """
    payload = {"target": dispositivo, "lineas": lineas}
    res = _peticion_http("POST", "/api/display", payload)
    if "error" in res:
        return res["error"]
    return f"Texto enviado a la pantalla de '{dispositivo}'."


# ============================================================================
# ESQUEMAS DE FUNCTION CALLING / TOOL CALLING PARA HERMES AGENT
# ============================================================================

_HERRAMIENTAS = [
    {
        "name": "listar_dispositivos_nuevos",
        "description": "Descubre microcontroladores ESP8266 recién encendidos que aún no han sido bautizados.",
        "properties": {},
        "required": [],
    },
    {
        "name": "identificar_dispositivo",
        "description": "Hace parpadear el LED azul de un dispositivo durante unos segundos para confirmar físicamente cuál tiene el usuario en la mano.",
        "properties": {
            "alias_o_mac": {"type": "string", "description": "Nombre amigable o dirección MAC del nodo a identificar."},
            "duracion_segundos": {"type": "integer", "description": "Segundos de parpadeo (por defecto 3)."},
        },
        "required": ["alias_o_mac"],
    },
    {
        "name": "registrar_dispositivo",
        "description": "Asigna un nombre en lenguaje humano y los pines conectados a un nuevo dispositivo ESP8266 ESP-01S.",
        "properties": {
            "alias": {"type": "string", "description": "Nombre humano amigable, ej: 'Pulsador del taller' o 'Luz de alerta'."},
            "mac": {"type": "string", "description": "Dirección MAC del dispositivo descubierta en balizas."},
            "funcion_dispositivo": {"type": "string", "description": "Breve descripción de qué hace el dispositivo en la casa."},
            "pin_sensor": {"type": ["string", "integer"], "description": "Pin del sensor o botón (ej: 'IO0', 'A0', o 0, 17)."},
            "pin_actuador": {"type": ["string", "integer"], "description": "Pin del actuador o LED (ej: 'IO2', 'LED', 'TX' o 2, 1)."},
            "tipo_sensor": {"type": "string", "enum": ["digital", "adc", "dht11", "dht22"], "description": "Naturaleza del sensor conectado; 'adc' habilita la lectura analógica."},
            "tipo_actuador": {"type": "string", "enum": ["digital", "pwm"], "description": "'pwm' permite atenuar el actuador con fade."},
        },
        "required": ["alias", "mac", "funcion_dispositivo"],
    },
    {
        "name": "activar_salida_temporal",
        "description": "Ejecuta una acción inmediata con duración de tiempo en un actuador (ej. encender luz 10 segundos).",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre amigable del dispositivo (ej. 'luz de alerta')."},
            "actuador": {"type": "string", "description": "Nombre del actuador o pin (ej. 'led', 'IO2')."},
            "duracion_segundos": {"type": "number", "description": "Duración en segundos de la acción."},
        },
        "required": ["dispositivo", "actuador", "duracion_segundos"],
    },
    {
        "name": "crear_automatizacion",
        "description": "Programa una regla autónoma permanente en el micro-PLC para que responda a botones o sensores sin requerir la Raspberry Pi.",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre del dispositivo donde se instalará la regla."},
            "pin_sensor": {"type": ["string", "integer"], "description": "Pin de entrada (ej: 'IO0', 'A0', o 0, 17)."},
            "condicion": {"type": "string", "description": "Condición: 'presionado', 'suelto', '==', '!=', '>', '<'."},
            "pin_actuador": {"type": ["string", "integer"], "description": "Pin de salida que se accionará (ej: 'IO2', 'LED', 2)."},
            "accion": {"type": "string", "description": "'encender' o 'apagar'."},
            "tipo_temporizador": {"type": "string", "description": "'inmediato', 'retardo' (TON), 'pulso', 'parpadeo' (blink), 'fade' (pwm) o 'heartbeat'."},
            "tiempo_segundos": {"type": "number", "description": "Segundos para el temporizador, si aplica."},
        },
        "required": ["dispositivo", "pin_sensor", "condicion", "pin_actuador"],
    },
    {
        "name": "consultar_dispositivos",
        "description": "Obtiene la lista de todos los dispositivos registrados, sus pines, sus tipos y el estado de su firma.",
        "properties": {},
        "required": [],
    },
    {
        "name": "borrar_reglas",
        "description": "Borra todas las reglas y automatizaciones de un dispositivo para detener su funcionamiento autónomo y apagar sus salidas.",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre amigable del dispositivo a limpiar."},
        },
        "required": ["dispositivo"],
    },
    {
        "name": "reconfigurar_dispositivo",
        "description": "Modifica un dispositivo ya registrado: cambia su nombre, su función o los pines conectados a él.",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre actual del dispositivo a modificar."},
            "nuevo_nombre": {"type": "string", "description": "Nuevo nombre si se desea renombrar."},
            "nueva_funcion": {"type": "string", "description": "Nueva descripción de la función."},
            "pin_sensor": {"type": ["string", "integer"], "description": "Nuevo pin del sensor o botón."},
            "pin_actuador": {"type": ["string", "integer"], "description": "Nuevo pin del actuador o LED."},
            "tipo_sensor": {"type": "string", "enum": ["digital", "adc", "dht11", "dht22"], "description": "Tipo del nuevo sensor."},
            "tipo_actuador": {"type": "string", "enum": ["digital", "pwm"], "description": "Tipo del nuevo actuador."},
        },
        "required": ["dispositivo"],
    },
    {
        "name": "desvincular_dispositivo",
        "description": "Elimina un dispositivo del sistema y resetea su microcontrolador para que pueda ser configurado desde cero.",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre del dispositivo a desvincular y resetear."},
        },
        "required": ["dispositivo"],
    },
    {
        "name": "crear_automatizacion_compuesta",
        "description": "Programa una regla con múltiples condiciones evaluadas con lógica AND u OR (ej. si boton1 presionado y boton2 suelto entonces parpadeo).",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre del dispositivo donde se instalará la regla."},
            "lista_condiciones": {
                "type": "array",
                "description": "Lista de condiciones. Cada una con 'pin' (ej: 'IO0', 'RX'), 'op' (==, !=, >, <), 'val', y opcional 'sub'.",
                "items": {"type": "object"},
            },
            "logica": {"type": "string", "enum": ["AND", "OR"], "description": "'AND' (todas deben cumplirse) o 'OR' (al menos una)."},
            "pin_actuador": {"type": ["string", "integer"], "description": "Pin de salida que se accionará (ej: 'IO2', 'LED')."},
            "accion": {"type": "string", "description": "'encender' o 'apagar'."},
            "tipo_temporizador": {"type": "string", "description": "'inmediato', 'retardo' (TON), 'pulso', 'parpadeo', 'fade' o 'heartbeat'."},
            "tiempo_segundos": {"type": "number", "description": "Segundos para el temporizador, si aplica."},
        },
        "required": ["dispositivo", "lista_condiciones", "pin_actuador"],
    },
    {
        "name": "mostrar_en_pantalla",
        "description": "Envía hasta 6 líneas de texto para mostrar en la pantalla OLED del nodo.",
        "properties": {
            "dispositivo": {"type": "string", "description": "Nombre del dispositivo con pantalla."},
            "lineas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista de hasta 6 textos a mostrar en el display.",
            },
        },
        "required": ["dispositivo", "lineas"],
    },
]

HERMES_TOOLS_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": _herramienta["name"],
            "description": _herramienta["description"],
            "parameters": {
                "type": "object",
                "properties": _herramienta["properties"],
                "required": _herramienta["required"],
            },
        },
    }
    for _herramienta in _HERRAMIENTAS
]

# Nombres expuestos, útiles para validar los esquemas en las pruebas.
NOMBRES_HERRAMIENTAS = [h["name"] for h in _HERRAMIENTAS]
