"""
config.py - Configuración, token de API y secreto compartido (Raspberry Pi).

Proyecto: Ena-creaccion (Capa Raspberry Pi)

- `ena_config.json` guarda el token de la API local y se crea solo la primera
  vez con 40 caracteres aleatorios. Se puede forzar con la variable de entorno
  `ENA_TOKEN`.
- El secreto compartido con los nodos se toma de `ENA_SECRET` o del archivo
  `ena_secreto.py` (una línea: `CLAVE = b"..."`). Sin secreto el sistema
  funciona en modo abierto, útil sólo para laboratorio.
"""
import importlib.util
import json
import os
import secrets
import threading

DIRECTORIO = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CONFIG = os.path.join(DIRECTORIO, "ena_config.json")
ARCHIVO_SECRETO = os.path.join(DIRECTORIO, "ena_secreto.py")

_LOCK = threading.RLock()
_CONFIG = None


def _guardar(datos):
    """Escritura atómica: nunca deja el JSON a medio escribir."""
    temporal = ARCHIVO_CONFIG + ".tmp"
    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, indent=2, ensure_ascii=False)
        archivo.flush()
        os.fsync(archivo.fileno())
    os.replace(temporal, ARCHIVO_CONFIG)


def cargar_config():
    """Lee (y crea si falta) la configuración del demonio."""
    global _CONFIG
    with _LOCK:
        if _CONFIG is not None:
            return _CONFIG

        datos = {}
        if os.path.exists(ARCHIVO_CONFIG):
            try:
                with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as archivo:
                    datos = json.load(archivo)
            except Exception as err:
                print(f"[Config] Aviso leyendo {ARCHIVO_CONFIG}: {err}")
                datos = {}
        if not isinstance(datos, dict):
            datos = {}

        if not datos.get("token"):
            datos["token"] = secrets.token_hex(20)
            try:
                _guardar(datos)
            except Exception as err:
                print(f"[Config] No se pudo guardar el token: {err}")

        datos.setdefault("host", "127.0.0.1")
        datos.setdefault("puerto", 8765)
        _CONFIG = datos
        return _CONFIG


def guardar_config(**cambios):
    """Actualiza claves concretas de la configuración."""
    global _CONFIG
    with _LOCK:
        datos = dict(cargar_config())
        datos.update(cambios)
        _guardar(datos)
        _CONFIG = datos
        return datos


def recargar():
    global _CONFIG
    with _LOCK:
        _CONFIG = None


def token():
    """Token exigido por la API local."""
    entorno = os.environ.get("ENA_TOKEN")
    if entorno:
        return entorno
    return cargar_config()["token"]


def clave():
    """
    Secreto compartido con los nodos, en bytes, o None si no hay.

    Prioridad: ENA_SECRET > rpi/ena_secreto.py.
    """
    entorno = os.environ.get("ENA_SECRET")
    if entorno:
        return entorno.encode("utf-8")

    if not os.path.exists(ARCHIVO_SECRETO):
        return None

    try:
        spec = importlib.util.spec_from_file_location("ena_secreto", ARCHIVO_SECRETO)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        valor = getattr(modulo, "CLAVE", None)
        if valor is None:
            return None
        if isinstance(valor, str):
            return valor.encode("utf-8")
        return bytes(valor)
    except Exception as err:
        print(f"[Config] No se pudo leer {ARCHIVO_SECRETO}: {err}")
        return None


def permitir_sin_firma():
    """Permite registrar nodos que no firman (sólo para laboratorio)."""
    valor = os.environ.get("ENA_PERMITIR_SIN_FIRMA", "")
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes")
