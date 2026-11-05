"""
framebuf.py - Simulador mínimo para poder importar ssd1306 en CPython.

Proyecto: Ena-creaccion (pruebas de la capa Raspberry Pi)
"""
MONO_VLSB = 0
RGB565 = 1
GS2_HMSB = 5


class FrameBuffer(object):
    def __init__(self, buffer, ancho, alto, formato=MONO_VLSB):
        self.buffer = buffer
        self.width = ancho
        self.height = alto
        self.formato = formato
        self.textos = []

    def fill(self, color):
        for indice in range(len(self.buffer)):
            self.buffer[indice] = color

    def pixel(self, x, y, color=None):
        if color is None:
            return 0
        return None

    def text(self, texto, x, y, color=1):
        self.textos.append((str(texto), x, y))

    def show_textos(self):
        return [t[0] for t in self.textos]
