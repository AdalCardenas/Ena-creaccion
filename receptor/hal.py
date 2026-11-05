"""
hal.py - Capa de Abstracción de Hardware (HAL) para ESP8266 / ESP-01S (Ena-receptor).

Proyecto: Ena-creaccion / Ena-receptor

Optimizado para ESP8266 ESP-01S (8 pines) y compatible con NodeMCU / D1 Mini:
- Nomenclatura flexible: 'IO0', 'IO2', 'RX', 'TX', 'LED', 'BOTON1', 'A0' o enteros.
- Lógica inversa automática para el LED azul en GPIO 2 (Active-LOW: 0=ON, 1=OFF).
- Gestión nativa de pull-up (Pin.PULL_DOWN no existe en ESP8266).
- PWM compatible con duty de 10 bits (0-1023) y 16 bits (duty_u16).
- SoftI2C seguro para ESP8266 (sin ID de bus rígido).
- El ADC del ESP8266 es único y vive en el canal 0; se expone como GPIO 17
  para que una regla pueda referirse a él como cualquier otro pin.

Además, este módulo no importa nada que no exista en CPython de forma directa:
si `machine` no está disponible (pruebas en la Raspberry Pi) se usa el
simulador inyectado en sys.path, lo que permite probar el motor de reglas
completo sin hardware.
"""
try:
    import machine
except ImportError:  # CPython sin el simulador de pruebas
    raise ImportError(
        "hal.py necesita el modulo machine de MicroPython "
        "o el simulador tests/sim en sys.path"
    )

try:
    from . import reloj
except ImportError:  # ejecución plana en el dispositivo
    import reloj

import sys

# Agregar /lib al sys.path para librerías auxiliares (ej. ssd1306)
if "/lib" not in sys.path:
    sys.path.append("/lib")

# El ADC del ESP8266 no corresponde a un GPIO físico: se le asigna el 17 como
# identificador lógico para que las reglas y descriptores lo traten como pin.
GPIO_ADC = 17

# GPIO 6..11 están conectados a la flash y 17 es sólo ADC: no pueden usarse
# como salida digital (intentarlo lanza ValueError y reinicia el nodo).
GPIO_RESERVADOS = (6, 7, 8, 9, 10, 11, 17)

# Mapeo de nombres físicos de pines en ESP-01S y ESP8266 genérico
MAPA_PINES = {
    # Pines directos del ESP-01S (8 pines)
    "IO0": 0, "GPIO0": 0, "0": 0, "BOTON1": 0, "D3": 0,
    "IO1": 1, "GPIO1": 1, "1": 1, "TX": 1, "TXD": 1,
    "IO2": 2, "GPIO2": 2, "2": 2, "LED": 2, "LED_AZUL": 2, "D4": 2,
    "IO3": 3, "GPIO3": 3, "3": 3, "RX": 3, "RXD": 3, "BOTON2": 3,

    # Mapeo retrocompatible para NodeMCU / Wemos D1 Mini
    "D0": 16, "D1": 5, "D2": 4, "D5": 14, "D6": 12, "D7": 13, "D8": 15,

    # Entrada analógica única del ESP8266
    "A0": GPIO_ADC, "ADC": GPIO_ADC, "AD": GPIO_ADC, "ANALOG": GPIO_ADC,
}


def resolver_pin(pin_id):
    """
    Convierte cualquier especificación de pin (string, alias, entero) al GPIO físico.
    Retorna el entero de GPIO o None si es inválido.
    """
    if pin_id is None:
        return None
    if isinstance(pin_id, int) and not isinstance(pin_id, bool):
        return pin_id
    clave = str(pin_id).strip().upper()
    if clave in MAPA_PINES:
        return MAPA_PINES[clave]
    try:
        return int(clave)
    except ValueError:
        return None


def es_salida_valida(pin_num):
    """True si el GPIO puede configurarse como salida digital."""
    return pin_num is not None and 0 <= pin_num <= 16 and pin_num not in GPIO_RESERVADOS


class HardwareManager:
    """Administrador centralizado de hardware y periféricos para ESP8266 / ESP-01S."""

    def __init__(self):
        self.pines_in = {}        # pin_num -> machine.Pin o machine.ADC
        self.pines_out = {}       # pin_num -> machine.Pin (OUT)
        self.pines_pwm = {}       # pin_num -> machine.PWM
        self.sensores_dht = {}    # pin_num -> {sensor, ultimo_tiempo, temp, hum, tipo}
        self.display_oled = None  # SSD1306_I2C si está conectado
        self.i2c_bus = None
        self._avisos = set()

    def _avisar(self, clave, mensaje):
        """Imprime cada aviso una sola vez para no inundar el puerto serie."""
        if clave in self._avisos:
            return
        self._avisos.add(clave)
        print(mensaje)

    # ========================================================================
    # INICIALIZACIÓN DE PERIFÉRICOS
    # ========================================================================

    def asegurar_pin_salida(self, pin_id):
        """Inicializa un pin como salida digital si no ha sido configurado."""
        pin_num = resolver_pin(pin_id)
        if pin_num is None:
            return None

        if not es_salida_valida(pin_num):
            self._avisar(("salida", pin_num),
                         f"[HAL] GPIO {pin_num} no puede usarse como salida.")
            return None

        if pin_num not in self.pines_out:
            try:
                p = machine.Pin(pin_num, machine.Pin.OUT)
            except Exception as err:
                self._avisar(("pin_out", pin_num),
                             f"[HAL] No se pudo configurar GPIO {pin_num}: {err}")
                return None
            # En GPIO 2 (LED azul ESP-01S) el nivel inicial apagado es 1 (Active-LOW)
            p.value(1 if pin_num == 2 else 0)
            self.pines_out[pin_num] = p

        return self.pines_out.get(pin_num)

    def asegurar_pin_entrada(self, pin_id, es_adc: bool = False, pull_up: bool = True):
        """
        Inicializa un pin como entrada digital (PULL_UP) o como ADC.

        `es_adc` lo declara el host en la lista "hw"; nunca se deduce del umbral
        de la regla. En ESP8266 el ADC es siempre el canal 0, expuesto aquí como
        GPIO_ADC para que sea direccionable.
        """
        pin_num = resolver_pin(pin_id)
        if pin_num is None:
            return None

        if pin_num not in self.pines_in:
            if es_adc or pin_num == GPIO_ADC:
                try:
                    adc = machine.ADC(0)
                    if hasattr(adc, "atten"):
                        adc.atten(machine.ADC.ATTN_11DB)
                    self.pines_in[pin_num] = adc
                except Exception as err:
                    self._avisar(("adc", pin_num), f"[HAL] Aviso ADC: {err}")
                    self.pines_in[pin_num] = None
            else:
                if pin_num in GPIO_RESERVADOS:
                    self._avisar(("entrada", pin_num),
                                 f"[HAL] GPIO {pin_num} no puede usarse como entrada.")
                    return None
                try:
                    if pull_up and hasattr(machine.Pin, "PULL_UP") and pin_num != 16:
                        p = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
                    else:
                        p = machine.Pin(pin_num, machine.Pin.IN)
                    self.pines_in[pin_num] = p
                except Exception as err:
                    self._avisar(("pin_in", pin_num),
                                 f"[HAL] Error configurando entrada GPIO {pin_num}: {err}")

        return self.pines_in.get(pin_num)

    def asegurar_dht(self, pin_id, es_dht22: bool = True):
        """Inicializa un sensor DHT11 o DHT22 con lectura protegida y caché."""
        pin_num = resolver_pin(pin_id)
        if pin_num is None or pin_num in GPIO_RESERVADOS:
            return

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
                    "tipo": "dht22" if es_dht22 else "dht11",
                }
                print(f"[HAL] Sensor DHT {'22' if es_dht22 else '11'} en GPIO {pin_num}")
            except Exception as err:
                self._avisar(("dht", pin_num),
                             f"[HAL] Error inicializando DHT en GPIO {pin_num}: {err}")

    def asegurar_oled(self, sda_pin=0, scl_pin=2, ancho: int = 128, alto: int = 64):
        """Inicializa una pantalla OLED SSD1306 vía SoftI2C para ESP8266."""
        if self.display_oled is not None:
            return self.display_oled

        sda = resolver_pin(sda_pin)
        scl = resolver_pin(scl_pin)

        try:
            from ssd1306 import SSD1306_I2C
            if hasattr(machine, "SoftI2C"):
                self.i2c_bus = machine.SoftI2C(
                    scl=machine.Pin(scl), sda=machine.Pin(sda), freq=400000
                )
            else:
                self.i2c_bus = machine.I2C(
                    scl=machine.Pin(scl), sda=machine.Pin(sda), freq=400000
                )

            self.display_oled = SSD1306_I2C(ancho, alto, self.i2c_bus)
            self.display_oled.fill(0)
            self.display_oled.text("Ena ESP-01S", 0, 0)
            self.display_oled.text("Listo", 0, 16)
            self.display_oled.show()
            print(f"[HAL] OLED SSD1306 inicializado (SDA={sda}, SCL={scl})")
            return self.display_oled
        except Exception as err:
            self._avisar("oled", f"[HAL] Aviso: no se detectó pantalla OLED ({err})")
            self.display_oled = None
            return None

    # ========================================================================
    # LECTURAS Y CONTROL
    # ========================================================================

    def leer_sensor(self, pin_id, sub_canal=None):
        """
        Lee el valor actual de un sensor:
        - Digital: 0 o 1.
        - DHT: sub_canal 'temp' o 'hum' (caché de 2 s).
        - ADC: 0-1023 (o 0 si no está disponible).
        """
        pin_num = resolver_pin(pin_id)
        if pin_num is None:
            return 0

        # Caso 1: Sensor DHT (temperatura / humedad con caché de 2 segundos)
        if pin_num in self.sensores_dht:
            info = self.sensores_dht[pin_num]
            ahora = reloj.ahora_ms()
            ultimo = info["ultimo_tiempo"]
            if ultimo == 0 or reloj.diferencia(ahora, ultimo) >= 2000:
                try:
                    info["sensor"].measure()
                    info["temp"] = info["sensor"].temperature()
                    info["hum"] = info["sensor"].humidity()
                    info["ultimo_tiempo"] = ahora
                except Exception:
                    pass

            canal = str(sub_canal).lower() if sub_canal is not None else "temp"
            if canal in ("hum", "h", "humedad", "1"):
                return info["hum"]
            return info["temp"]

        # Caso 2: Pin estándar (digital o ADC)
        obj = self.pines_in.get(pin_num)
        if obj is None:
            return 0

        try:
            if isinstance(obj, machine.ADC):
                return obj.read()
            return obj.value()
        except Exception:
            return 0

    def aplicar_pwm(self, pin_id, duty_ratio: float):
        """
        Aplica un ciclo de trabajo PWM de 0.0 a 1.0 (1000 Hz).
        Maneja la inversión automática del LED azul en GPIO 2 (Active-LOW).
        """
        pin_num = resolver_pin(pin_id)
        if not es_salida_valida(pin_num):
            return

        # Inversión para Active-LOW en GPIO 2: brillo 1.0 equivale a duty 0.0
        duty = (1.0 - duty_ratio) if pin_num == 2 else duty_ratio
        duty = max(0.0, min(1.0, duty))

        if pin_num not in self.pines_pwm:
            try:
                p = machine.Pin(pin_num, machine.Pin.OUT)
                self.pines_pwm[pin_num] = machine.PWM(p, freq=1000)
            except Exception as err:
                self._avisar(("pwm", pin_num),
                             f"[HAL] Error iniciando PWM en GPIO {pin_num}: {err}")
                return

        pwm = self.pines_pwm.get(pin_num)
        if pwm is None:
            return

        try:
            if hasattr(pwm, "duty_u16"):
                pwm.duty_u16(int(duty * 65535))
            elif hasattr(pwm, "duty"):
                pwm.duty(int(duty * 1023))
        except Exception as err:
            self._avisar(("pwm_duty", pin_num), f"[HAL] Error aplicando PWM: {err}")

    def aplicar_salida(self, pin_id, accion: int):
        """
        Aplica un estado lógico sobre un pin de salida:
        - accion: 0=Apagar, 1=Encender, 2=Conmutar (Toggle).
        Traduce automáticamente la lógica inversa del LED azul en GPIO 2.
        """
        pin_num = resolver_pin(pin_id)
        if not es_salida_valida(pin_num):
            return

        # Si el pin estaba en modo PWM y se pide un estado estático
        if pin_num in self.pines_pwm:
            if accion == 0:
                self.aplicar_pwm(pin_num, 0.0)
                return
            if accion == 1:
                self.aplicar_pwm(pin_num, 1.0)
                return
            try:
                self.pines_pwm[pin_num].deinit()
            except Exception:
                pass
            self.pines_pwm.pop(pin_num, None)

        pin = self.pines_out.get(pin_num)
        if not pin:
            pin = self.asegurar_pin_salida(pin_num)
            if not pin:
                return

        invertido = (pin_num == 2)
        try:
            if accion == 0:
                pin.value(1 if invertido else 0)
            elif accion == 1:
                pin.value(0 if invertido else 1)
            elif accion == 2:
                pin.value(0 if pin.value() else 1)
        except Exception as err:
            self._avisar(("salida", pin_num), f"[HAL] Error aplicando salida: {err}")

    def apagar_todas_las_salidas(self):
        """Apaga de forma segura todas las salidas digitales y canales PWM."""
        for pin_num, pwm in list(self.pines_pwm.items()):
            try:
                duty_fisico = 65535 if pin_num == 2 else 0
                if hasattr(pwm, "duty_u16"):
                    pwm.duty_u16(duty_fisico)
                elif hasattr(pwm, "duty"):
                    pwm.duty(1023 if pin_num == 2 else 0)
            except Exception:
                pass

        for pin_num, p in self.pines_out.items():
            try:
                p.value(1 if pin_num == 2 else 0)
            except Exception:
                pass

    def mostrar_en_oled(self, lineas):
        """Dibuja hasta 6 líneas de texto (16 caracteres) en la pantalla OLED."""
        if not isinstance(lineas, (list, tuple)):
            return False

        if not self.display_oled:
            self.asegurar_oled(sda_pin=0, scl_pin=2)

        if not self.display_oled:
            return False

        try:
            self.display_oled.fill(0)
            y = 0
            for linea in list(lineas)[:6]:
                self.display_oled.text(str(linea)[:16], 0, y)
                y += 10
            self.display_oled.show()
            return True
        except Exception as err:
            self._avisar("oled_pintar", f"[HAL] Error actualizando OLED: {err}")
            return False
