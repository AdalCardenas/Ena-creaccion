"""
device_registry.py - Registro semántico y almacenamiento de alias de dispositivos.

Proyecto: Ena-creaccion (Capa Raspberry Pi)

Dos correcciones importantes frente a la primera versión:

1. Concurrencia: el hilo que lee el puerto serie y los hilos HTTP escribían el
   mismo `devices.json` sin coordinación, así que dos altas simultáneas podían
   perder datos o corromper el archivo. Todas las operaciones pasan ahora por
   un `RLock` y el guardado es atómico (temporal + `os.replace`).
2. Metadatos: cada pin declara su tipo (`digital_in`, `adc`, `dht22`,
   `digital_out`, `oled`). El nodo ya no deduce el modo del pin a partir del
   umbral de la regla; `descriptores_hw()` genera la lista `hw` de la trama.
"""
import json
import os
import re
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

REGISTRO_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")

# Tipos de pin admitidos en el registro.
TIPOS_PIN = (
    "digital_in", "adc", "dht11", "dht22",
    "digital_out", "pwm", "oled",
)

TIPOS_ADC = ("adc", "analog", "analogico", "analógica")
TIPOS_DHT = ("dht11", "dht22")
TIPOS_OLED = ("oled", "ssd1306")


def normalizar_tipo(tipo, por_omision="digital_in"):
    """Traduce alias de tipo ('analogico', 'rele', 'boton') al tipo canónico."""
    if not tipo:
        return por_omision
    clave = str(tipo).strip().lower()
    if clave in TIPOS_ADC:
        return "adc"
    if clave in TIPOS_DHT:
        return clave
    if clave in TIPOS_OLED:
        return "oled"
    if clave in ("digital_out", "salida", "rele", "relay", "led", "pwm", "actuador"):
        return "pwm" if clave == "pwm" else "digital_out"
    if clave in ("digital_in", "digital", "entrada", "boton", "button", "pulsador", "sensor"):
        return "digital_in"
    return por_omision


# Nombres de pin que indican actuador cuando el registro no trae tipo.
_NOMBRES_SALIDA = (
    "actuador", "rele", "relé", "relay", "led", "foco", "lampara", "lámpara",
    "valvula", "válvula", "bomba", "motor", "salida", "sirena", "ventilador",
)


def tipo_por_nombre(nombre, por_omision="digital_in"):
    """Deduce el tipo por el nombre del pin (no por su valor ni su umbral)."""
    clave = str(nombre).strip().lower()
    for palabra in _NOMBRES_SALIDA:
        if palabra in clave:
            return "digital_out"
    return por_omision


class DeviceRegistry:
    """Administra la asignación de nombres humanos a direcciones MAC y pines."""

    def __init__(self, filepath: str = REGISTRO_DEFAULT):
        self.filepath = filepath
        self._lock = threading.RLock()
        self.dispositivos: Dict[str, Any] = {}
        self.nodos_pendientes: Dict[str, Any] = {}
        self.cargar()

    # =====================================================================
    # PERSISTENCIA
    # =====================================================================

    def cargar(self):
        """Carga el archivo devices.json si existe."""
        with self._lock:
            if not os.path.exists(self.filepath):
                self.dispositivos = {}
                return
            try:
                with open(self.filepath, "r", encoding="utf-8") as archivo:
                    datos = json.load(archivo)
                self.dispositivos = datos if isinstance(datos, dict) else {}
            except Exception as err:
                print(f"[Registro] Error cargando {self.filepath}: {err}")
                self.dispositivos = {}

    def guardar(self):
        """Guarda la base de datos de forma atómica (nunca a medio escribir)."""
        with self._lock:
            temporal = self.filepath + ".tmp"
            try:
                with open(temporal, "w", encoding="utf-8") as archivo:
                    json.dump(self.dispositivos, archivo, indent=2, ensure_ascii=False)
                    archivo.flush()
                    os.fsync(archivo.fileno())
                os.replace(temporal, self.filepath)
            except Exception as err:
                print(f"[Registro] Error guardando {self.filepath}: {err}")
                try:
                    if os.path.exists(temporal):
                        os.remove(temporal)
                except OSError:
                    pass

    # =====================================================================
    # DESCUBRIMIENTO
    # =====================================================================

    def registrar_baliza(self, mac: str, datos_baliza: dict, autenticado=None):
        """Registra o actualiza una baliza recibida de un nodo en campo."""
        mac_limpia = str(mac).upper().strip()
        with self._lock:
            self.nodos_pendientes[mac_limpia] = {
                "mac": mac_limpia,
                "ultima_deteccion": time.time(),
                "status": datos_baliza.get("status", "desconocido"),
                "reglas": datos_baliza.get("reglas", 0),
                "chip": datos_baliza.get("chip", "desconocido"),
                "autenticado": autenticado,
            }

    def listar_pendientes(self) -> List[Dict[str, Any]]:
        """Nodos descubiertos que aún no han sido registrados."""
        with self._lock:
            macs_registradas = {
                info["mac"].upper() for info in self.dispositivos.values()
            }
            pendientes = []
            for mac, info in self.nodos_pendientes.items():
                if mac not in macs_registradas:
                    pendientes.append(dict(info))
            return pendientes

    def estado_autenticacion(self, mac: str):
        """None si no hay secreto configurado, True/False según la baliza."""
        with self._lock:
            info = self.nodos_pendientes.get(str(mac).upper().strip())
            if not info:
                return None
            return info.get("autenticado")

    # =====================================================================
    # REGISTRO Y BÚSQUEDA
    # =====================================================================

    def normalizar_clave(self, texto: str) -> str:
        """
        Convierte 'Luz del Lavaloza' a 'luz_del_lavaloza'.

        También quita acentos, para que 'Botón Entrada' y 'boton entrada'
        apunten al mismo dispositivo.
        """
        base = unicodedata.normalize("NFKD", str(texto).lower())
        sin_acentos = "".join(c for c in base if not unicodedata.combining(c))
        texto_limpio = re.sub(r"[^\w\s]", "", sin_acentos).strip()
        return re.sub(r"\s+", "_", texto_limpio)

    def registrar_dispositivo(self, alias: str, mac: str, pines: dict,
                              descripcion: str = "") -> Dict[str, Any]:
        """
        Asocia un alias humano con una dirección MAC y sus pines.
        Si la MAC ya existía bajo otro nombre, la reasigna limpiamente.
        """
        clave = self.normalizar_clave(alias)
        mac_limpia = str(mac).upper().strip()
        pines_normalizados = self._normalizar_pines(pines)

        with self._lock:
            claves_duplicadas = [
                k for k, v in self.dispositivos.items() if v.get("mac") == mac_limpia
            ]
            reglas_previas = []
            for k in claves_duplicadas:
                reglas_previas = self.dispositivos[k].get("reglas_activas", [])
                del self.dispositivos[k]

            autenticado = None
            pendiente = self.nodos_pendientes.get(mac_limpia)
            if pendiente:
                autenticado = pendiente.get("autenticado")

            registro = {
                "clave": clave,
                "alias": alias,
                "mac": mac_limpia,
                "descripcion": descripcion,
                "pines": pines_normalizados,
                "reglas_activas": reglas_previas,
                "autenticado": autenticado,
                "fecha_registro": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            self.dispositivos[clave] = registro
            self.guardar()
            self.nodos_pendientes.pop(mac_limpia, None)
            return dict(registro)

    def _normalizar_pines(self, pines) -> Dict[str, Any]:
        """Convierte cualquier forma de pin en {'pin': x, 'tipo': y}."""
        resultado: Dict[str, Any] = {}
        if not isinstance(pines, dict):
            return resultado

        for nombre, info in pines.items():
            if isinstance(info, dict):
                item = dict(info)
                item["tipo"] = normalizar_tipo(
                    item.get("tipo"), tipo_por_nombre(nombre)
                )
                resultado[nombre] = item
            elif isinstance(info, (int, str)):
                resultado[nombre] = {
                    "pin": info,
                    "tipo": tipo_por_nombre(nombre),
                }
        return resultado

    def reconfigurar_dispositivo(self, alias_o_mac: str, nuevo_alias: str = None,
                                 nuevos_pines: dict = None,
                                 nueva_descripcion: str = None) -> Optional[Dict[str, Any]]:
        """Modifica un dispositivo existente: nombre, pines o descripción."""
        with self._lock:
            disp = self.buscar_dispositivo(alias_o_mac)
            if not disp:
                return None

            clave_antigua = disp["clave"]

            if nuevo_alias:
                disp["alias"] = nuevo_alias
                nueva_clave = self.normalizar_clave(nuevo_alias)
                disp["clave"] = nueva_clave
                if nueva_clave != clave_antigua:
                    self.dispositivos.pop(clave_antigua, None)
                    self.dispositivos[nueva_clave] = disp

            if nuevos_pines is not None:
                disp["pines"] = self._normalizar_pines(nuevos_pines)

            if nueva_descripcion is not None:
                disp["descripcion"] = nueva_descripcion

            self.guardar()
            return dict(disp)

    def buscar_dispositivo(self, alias_o_mac: str) -> Optional[Dict[str, Any]]:
        """Busca por alias exacto, MAC, o coincidencia parcial del alias."""
        if not alias_o_mac:
            return None
        termino = str(alias_o_mac).strip().lower()
        termino_clave = self.normalizar_clave(alias_o_mac)

        with self._lock:
            if termino_clave in self.dispositivos:
                return self.dispositivos[termino_clave]

            for disp in self.dispositivos.values():
                if str(disp.get("alias", "")).lower() == termino:
                    return disp
                if str(disp.get("mac", "")).lower() == termino:
                    return disp

            for disp in self.dispositivos.values():
                if termino in str(disp.get("alias", "")).lower():
                    return disp
                if termino in str(disp.get("clave", "")):
                    return disp

        return None

    # =====================================================================
    # REGLAS ACTIVAS Y HARDWARE
    # =====================================================================

    def actualizar_reglas(self, alias_o_mac: str, reglas: list) -> bool:
        with self._lock:
            disp = self.buscar_dispositivo(alias_o_mac)
            if not disp:
                return False
            disp["reglas_activas"] = list(reglas)
            self.guardar()
            return True

    def limpiar_reglas(self, alias_o_mac: str) -> bool:
        return self.actualizar_reglas(alias_o_mac, [])

    def descriptores_hw(self, dispositivo) -> List[Dict[str, Any]]:
        """
        Genera la lista `hw` que viaja en la trama para inicializar el hardware.

        Sin esta declaración el nodo trataría todos los pines como entradas
        digitales: un sensor analógico comparado contra 0/1 nunca funcionaba.
        """
        if isinstance(dispositivo, dict):
            disp = dispositivo
        else:
            disp = self.buscar_dispositivo(dispositivo) or {}

        descriptores = []
        pines = disp.get("pines") or {}
        for nombre, info in pines.items():
            if not isinstance(info, dict):
                info = {"pin": info}
            descriptor = {
                "pin": info.get("pin"),
                "tipo": normalizar_tipo(info.get("tipo"), tipo_por_nombre(nombre)),
            }
            for extra in ("sda", "scl", "ancho", "alto"):
                if extra in info:
                    descriptor[extra] = info[extra]
            descriptores.append(descriptor)
        return descriptores

    def eliminar_dispositivo(self, alias_o_mac: str) -> Optional[Dict[str, Any]]:
        """Elimina el dispositivo del registro para dejarlo disponible como nuevo."""
        with self._lock:
            disp = self.buscar_dispositivo(alias_o_mac)
            if not disp:
                return None
            self.dispositivos.pop(disp["clave"], None)
            self.guardar()
            return dict(disp)

    def listar_dispositivos(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(d) for d in self.dispositivos.values()]
