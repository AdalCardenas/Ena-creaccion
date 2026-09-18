"""
hal.py - Capa de Abstracción de Hardware (HAL) para el Receptor Ena.
Proyecto: Ena-creaccion / Ena-receptor

Administra de manera unificada y tolerante a fallos:
- Pines digitales (entrada y salida con pull-down)
- Pines analógicos (ADC con atenuación a 3.3V)
- Sensores de temperatura y humedad DHT11/DHT22 (con lectura cacheada)
- Pantallas OLED I2C (SSD1306)
"""
import machine
import time
import sys

# Agregar /lib a sys.path si no está
if "/lib" not in sys.path:
    sys.path.append("/lib")


class HardwareManager:
    """Administrador centralizado de hardware y periféricos."""

    def __init__(self):
        self.pines_in = {}       # pin_num -> Pin o ADC
        self.pines_out = {}      # pin_num -> Pin OUT
        self.sensores_dht = {}   # pin_num -> {objeto, ultimo_tiempo, temp, hum, tipo}
        self.display_oled = None # Instancia SSD1306 si está activa
        self.i2c_bus = None

    # ========================================================================
    # INICIALIZACIÓN DE PERIFÉRICOS
    # ========================================================================

    def asegurar_pin_salida(self, pin_num: int):
        """Inicializa un pin como salida si no existe."""
        if pin_num is not None and pin_num >= 0 and pin_num not in self.pines_out:
            p = machine.Pin(pin_num, machine.Pin.OUT)
            p.value(0)
            self.pines_out[pin_num] = p
        return self.pines_out.get(pin_num)

    def asegurar_pin_entrada(self, pin_num: int, es_adc: bool = False):
        """Inicializa un pin como entrada digital o ADC analógico."""
        if pin_num is None or pin_num < 0:
            return None

        if pin_num not in self.pines_in:
            if es_adc:
                adc = machine.ADC(machine.Pin(pin_num))
                if hasattr(adc, 'atten'):
                    adc.atten(machine.ADC.ATTN_11DB)  # Rango 0-3.3V (0-4095)
                self.pines_in[pin_num] = adc
            else:
                self.pines_in[pin_num] = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_DOWN)

        return self.pines_in.get(pin_num)

    def asegurar_dht(self, pin_num: int, es_dht22: bool = True):
        """Inicializa un sensor DHT11 o DHT22 con caché para lecturas seguras."""
        if pin_num not in self.sensores_dht:
            try:
                import dht
                pin_obj = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
                sensor_obj = dht.DHT22(pin_obj) if es_dht22 else dht.DHT11(pin_obj)
                self.sensores_dht[pin_num] = {
                    "sensor": sensor_obj,
                    "ultimo_tiempo": 0,
                    "temp": 0.0,
                    "hum": 0.0,
                    "tipo": "dht22" if es_dht22 else "dht11"
                }
                print(f"[HAL] Sensor DHT {'22' if es_dht22 else '11'} inicializado en pin {pin_num}")
            except Exception as e:
                print(f"[HAL] Error inicializando DHT en pin {pin_num}: {e}")

    def asegurar_oled(self, sda_pin: int = 21, scl_pin: int = 22, ancho: int = 128, alto: int = 64):
        """Inicializa una pantalla OLED SSD1306 vía I2C."""
        if self.display_oled is not None:
            return self.display_oled

        try:
            from ssd1306 import SSD1306_I2C
            self.i2c_bus = machine.I2C(0, scl=machine.Pin(scl_pin), sda=machine.Pin(sda_pin), freq=400000)
            self.display_oled = SSD1306_I2C(ancho, alto, self.i2c_bus)
            self.display_oled.fill(0)
            self.display_oled.text("Ena Receptor", 0, 0)
            self.display_oled.text("En linea", 0, 16)
            self.display_oled.show()
            print(f"[HAL] Pantalla OLED SSD1306 inicializada (SDA={sda_pin}, SCL={scl_pin})")
            return self.display_oled
        except Exception as e:
            print(f"[HAL] Aviso: No se detectó pantalla OLED en I2C ({e})")
            self.display_oled = None
            return None

    # ========================================================================
    # LECTURAS Y CONTROL
    # ========================================================================

    def leer_sensor(self, pin_num: int, sub_canal=None):
        """
        Lee el valor de un sensor:
        - Si es DHT: sub_canal puede ser 'temp'/'t'/0 o 'hum'/'h'/1.
        - Si es ADC: devuelve lectura 0-4095.
        - Si es Digital: devuelve 0 o 1.
        """
        # Caso 1: Sensor DHT (Temperatura / Humedad)
        if pin_num in self.sensores_dht:
            info = self.sensores_dht[pin_num]
            ahora = time.ticks_ms()
            # El sensor DHT requiere al menos 2 segundos entre lecturas físicas
            if time.ticks_diff(ahora, info["ultimo_tiempo"]) >= 2000 or info["ultimo_tiempo"] == 0:
                try:
                    info["sensor"].measure()
                    info["temp"] = info["sensor"].temperature()
                    info["hum"] = info["sensor"].humidity()
                    info["ultimo_tiempo"] = ahora
                except Exception as err:
                    # En caso de error de lectura (frecuente en cables largos), conserva el último valor
                    pass

            canal_str = str(sub_canal).lower() if sub_canal is not None else "temp"
            if canal_str in ["hum", "h", "humedad", "1"]:
                return info["hum"]
            return info["temp"]

        # Caso 2: Pin estándar (Digital o ADC)
        obj = self.pines_in.get(pin_num)
        if obj is None:
            return 0

        if isinstance(obj, machine.ADC):
            return obj.read()
        return obj.value()

    def aplicar_salida(self, pin_num: int, accion: int):
        """Aplica estado sobre un pin de salida (0=LOW, 1=HIGH, 2=Toggle)."""
        pin = self.pines_out.get(pin_num)
        if not pin:
            return
        if accion == 0:
            pin.value(0)
        elif accion == 1:
            pin.value(1)
        elif accion == 2:
            pin.value(not pin.value())

    def apagar_todas_las_salidas(self):
        """Pone todas las salidas en 0 de forma segura."""
        for p in self.pines_out.values():
            try:
                p.value(0)
            except:
                pass

    def mostrar_en_oled(self, lineas: list):
        """Dibuja hasta 6 líneas de texto en la pantalla OLED."""
        if not self.display_oled:
            # Intento de inicializar con pines estándar ESP32
            self.asegurar_oled(sda_pin=21, scl_pin=22)

        if not self.display_oled:
            return False

        try:
            self.display_oled.fill(0)
            y = 0
            for linea in lineas[:6]:
                self.display_oled.text(str(linea)[:16], 0, y)
                y += 10
            self.display_oled.show()
            return True
        except Exception as e:
            print(f"[HAL] Error actualizando OLED: {e}")
            return False
