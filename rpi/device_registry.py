"""
device_registry.py - Registro semántico y almacenamiento de alias de dispositivos.
Proyecto: Ena-creaccion (Capa Raspberry Pi)
"""
import os
import json
import time
from typing import Optional, Dict, Any, List

REGISTRO_DEFAULT = os.path.join(os.path.dirname(__file__), "devices.json")


class DeviceRegistry:
    """Administra la asignación de nombres humanos a direcciones MAC y pines."""

    def __init__(self, filepath: str = REGISTRO_DEFAULT):
        self.filepath = filepath
        self.dispositivos: Dict[str, Any] = {}
        self.nodos_pendientes: Dict[str, Any] = {}  # MAC -> info de baliza
        self.cargar()

    def cargar(self):
        """Carga el archivo devices.json si existe."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.dispositivos = json.load(f)
            except Exception as e:
                print(f"[Registro] Error cargando {self.filepath}: {e}")
                self.dispositivos = {}
        else:
            self.dispositivos = {}

    def guardar(self):
        """Guarda la base de datos en devices.json."""
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.dispositivos, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Registro] Error guardando {self.filepath}: {e}")

    def registrar_baliza(self, mac: str, datos_baliza: dict):
        """Registra o actualiza una baliza recibida de un nodo en campo."""
        mac_limpia = mac.upper().strip()
        self.nodos_pendientes[mac_limpia] = {
            "mac": mac_limpia,
            "ultima_deteccion": time.time(),
            "status": datos_baliza.get("status", "desconocido"),
            "reglas": datos_baliza.get("reglas", 0)
        }

    def listar_pendientes(self) -> List[Dict[str, Any]]:
        """Retorna la lista de nodos descubiertos que aún no han sido registrados."""
        macs_registradas = {info["mac"].upper() for info in self.dispositivos.values()}
        pendientes = []
        for mac, info in self.nodos_pendientes.items():
            if mac not in macs_registradas:
                pendientes.append(info)
        return pendientes

    def normalizar_clave(self, texto: str) -> str:
        """Convierte 'Luz del Lavaloza' a 'luz_del_lavaloza'."""
        import re
        texto_limpio = re.sub(r'[^\w\s]', '', texto.lower()).strip()
        return re.sub(r'\s+', '_', texto_limpio)

    def registrar_dispositivo(self, alias: str, mac: str, pines: dict,
                              descripcion: str = "") -> Dict[str, Any]:
        """
        Asocia un alias humano con una dirección MAC y sus pines.
        Si la MAC ya existía bajo otro nombre, la reasigna limpiamente.
        """
        clave = self.normalizar_clave(alias)
        mac_limpia = mac.upper().strip()

        # Limpiar si la MAC ya existía registrada bajo otro alias anterior
        claves_duplicadas = [k for k, v in self.dispositivos.items() if v["mac"] == mac_limpia]
        reglas_previas = []
        for k in claves_duplicadas:
            reglas_previas = self.dispositivos[k].get("reglas_activas", [])
            del self.dispositivos[k]

        registro = {
            "clave": clave,
            "alias": alias,
            "mac": mac_limpia,
            "descripcion": descripcion,
            "pines": pines,  # Ej: {"sensor": 4, "foco": 2}
            "reglas_activas": reglas_previas,
            "fecha_registro": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        self.dispositivos[clave] = registro
        self.guardar()

        # Remover de pendientes si estaba ahí
        if mac_limpia in self.nodos_pendientes:
            del self.nodos_pendientes[mac_limpia]

        return registro

    def reconfigurar_dispositivo(self, alias_o_mac: str, nuevo_alias: str = None,
                                 nuevos_pines: dict = None,
                                 nueva_descripcion: str = None) -> Optional[Dict[str, Any]]:
        """
        Permite reprogramar o modificar un dispositivo ya existente (cambio de nombre o pines).
        """
        disp = self.buscar_dispositivo(alias_o_mac)
        if not disp:
            return None

        clave_antigua = disp["clave"]

        if nuevo_alias:
            disp["alias"] = nuevo_alias
            nueva_clave = self.normalizar_clave(nuevo_alias)
            disp["clave"] = nueva_clave
            if nueva_clave != clave_antigua:
                del self.dispositivos[clave_antigua]
                self.dispositivos[nueva_clave] = disp

        if nuevos_pines is not None:
            disp["pines"] = nuevos_pines

        if nueva_descripcion is not None:
            disp["descripcion"] = nueva_descripcion

        self.guardar()
        return disp

    def buscar_dispositivo(self, alias_o_mac: str) -> Optional[Dict[str, Any]]:
        """
        Busca un dispositivo por alias exacto, coincidencia parcial o dirección MAC.
        """
        termino = alias_o_mac.strip().lower()
        termino_clave = self.normalizar_clave(alias_o_mac)

        # 1. Búsqueda por clave o alias exacto
        if termino_clave in self.dispositivos:
            return self.dispositivos[termino_clave]

        for disp in self.dispositivos.values():
            if disp["alias"].lower() == termino or disp["mac"].lower() == termino:
                return disp

        # 2. Búsqueda por coincidencia parcial (ej. 'aspersor' encuentra 'aspersor del jardín')
        for disp in self.dispositivos.values():
            if termino in disp["alias"].lower() or termino in disp["clave"]:
                return disp

        return None

    def actualizar_reglas(self, alias_o_mac: str, reglas: list) -> bool:
        """Guarda las reglas actualmente aplicadas en el dispositivo."""
        disp = self.buscar_dispositivo(alias_o_mac)
        if not disp:
            return False
        disp["reglas_activas"] = reglas
        self.guardar()
        return True

    def limpiar_reglas(self, alias_o_mac: str) -> bool:
        """Borra la lista de reglas activas del dispositivo en el registro."""
        return self.actualizar_reglas(alias_o_mac, [])

    def eliminar_dispositivo(self, alias_o_mac: str) -> Optional[Dict[str, Any]]:
        """
        Elimina por completo un dispositivo del registro para dejarlo disponible como nuevo.
        """
        disp = self.buscar_dispositivo(alias_o_mac)
        if not disp:
            return None

        clave = disp["clave"]
        del self.dispositivos[clave]
        self.guardar()
        return disp

    def listar_dispositivos(self) -> List[Dict[str, Any]]:
        """Retorna todos los dispositivos registrados."""
        return list(self.dispositivos.values())
