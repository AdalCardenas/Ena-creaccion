"""
test_protocolo.py - Autenticidad, canonicalización y lotes del protocolo v2.

Proyecto: Ena-creaccion

Verifica que la Raspberry Pi y el nodo producen exactamente los mismos bytes
(HMAC y serialización canónica) y que las transacciones de reglas se reparten
sin superar el límite de ESP-NOW.
"""
import hashlib
import hmac
import json
import unittest

from tests import util

util.preparar_entorno()

import protocolo as host  # noqa: E402  (ruta preparada justo arriba)


CLAVE = b"secreto-de-pruebas-0123456789"
MAC = "A0:20:A6:11:22:33"


class TestCanonizacion(unittest.TestCase):
    def setUp(self):
        self.nodo = util.modulo_nodo("protocolo")

    def test_mismos_bytes_en_ambos_extremos(self):
        casos = [
            {},
            {"r": []},
            {"cmd": "tx_chunk", "id": 7, "k": 0, "r": [[1, 4, 0, 1, 2, 1, 0, 0]]},
            {"r": [[1, [[[4, "temp"], 2, 28], [13, 0, 1]], 0, 0, 15, 1, 0, 0]]},
            {"lineas": ["Temp: 24 C", "Aire: ON"]},
            {"a": True, "b": False, "c": None, "d": -1},
            {"hw": [{"pin": 2, "tipo": "digital_out"}]},
        ]
        for caso in casos:
            self.assertEqual(
                host.canonizar(caso), self.nodo.canonizar(caso),
                msg="difieren para %r" % (caso,)
            )

    def test_orden_de_claves_no_importa(self):
        uno = {"zeta": 1, "alfa": 2}
        otro = {"alfa": 2, "zeta": 1}
        self.assertEqual(host.canonizar(uno), host.canonizar(otro))
        self.assertEqual(host.canonizar(uno), '{"alfa":2,"zeta":1}')

    def test_rechaza_no_ascii(self):
        with self.assertRaises(host.ErrorProtocolo):
            host.canonizar({"alias": "Luz del lavadero ñ"})
        with self.assertRaises(self.nodo.ErrorProtocolo):
            self.nodo.canonizar({"alias": "ñ"})

    def test_rechaza_flotantes(self):
        with self.assertRaises(host.ErrorProtocolo):
            host.canonizar(1.5)

    def test_escapa_comillas_y_controles(self):
        self.assertEqual(host.canonizar('a"b'), '"a\\"b"')
        self.assertEqual(host.canonizar("a\\b"), '"a\\\\b"')
        self.assertEqual(host.canonizar("\n"), '"\\u000a"')

    def test_hmac_coincide_con_la_biblioteca_estandar(self):
        self.assertEqual(
            host.hmac_sha256(CLAVE, b"mensaje"),
            hmac.new(CLAVE, b"mensaje", hashlib.sha256).digest(),
        )
        # Clave más larga que el bloque: debe hashearse primero (RFC 2104).
        clave_larga = b"x" * 100
        self.assertEqual(
            host.hmac_sha256(clave_larga, b"m"),
            hmac.new(clave_larga, b"m", hashlib.sha256).digest(),
        )

    def test_firma_identica_y_truncada(self):
        paquete = {"cmd": "tx_commit", "id": 7}
        a = host.firmar(CLAVE, MAC, 123456789, paquete)
        b = self.nodo.firmar(CLAVE, MAC, 123456789, paquete)
        self.assertEqual(a, b)
        self.assertEqual(len(a["h"]), 16)
        self.assertEqual(a["n"], 123456789)
        self.assertNotIn("h", paquete)  # no muta el original


class TestVerificacion(unittest.TestCase):
    def setUp(self):
        self.nodo = util.modulo_nodo("protocolo")
        self.paquete = host.firmar(CLAVE, MAC, 100, {"cmd": "ping"})

    def test_acepta_firma_valida(self):
        ok, motivo, contador = self.nodo.verificar(CLAVE, MAC, self.paquete, 0)
        self.assertTrue(ok, motivo)
        self.assertEqual(contador, 100)

    def test_rechaza_replay(self):
        ok, motivo, _ = self.nodo.verificar(CLAVE, MAC, self.paquete, 100)
        self.assertFalse(ok)
        self.assertEqual(motivo, "replay")

    def test_rechaza_otro_mac(self):
        ok, motivo, _ = self.nodo.verificar(CLAVE, "AA:BB:CC:DD:EE:FF", self.paquete, 0)
        self.assertFalse(ok)
        self.assertEqual(motivo, "firma_invalida")

    def test_rechaza_cuerpo_alterado(self):
        alterado = dict(self.paquete)
        alterado["cmd"] = "display"
        ok, motivo, _ = self.nodo.verificar(CLAVE, MAC, alterado, 0)
        self.assertFalse(ok)
        self.assertEqual(motivo, "firma_invalida")

    def test_rechaza_otro_secreto(self):
        ok, motivo, _ = self.nodo.verificar(b"otro-secreto", MAC, self.paquete, 0)
        self.assertFalse(ok)
        self.assertEqual(motivo, "firma_invalida")

    def test_rechaza_sin_firma(self):
        ok, motivo, _ = self.nodo.verificar(CLAVE, MAC, {"cmd": "ping"}, 0)
        self.assertFalse(ok)
        self.assertEqual(motivo, "sin_firma")

    def test_rechaza_firma_corta(self):
        ok, motivo, _ = self.nodo.verificar(
            CLAVE, MAC, {"cmd": "ping", "n": 5, "h": "abc"}, 0
        )
        self.assertFalse(ok)
        self.assertEqual(motivo, "sin_firma")

    def test_rechaza_no_objeto(self):
        ok, motivo, _ = self.nodo.verificar(CLAVE, MAC, [1, 2, 3], 0)
        self.assertFalse(ok)
        self.assertEqual(motivo, "no_es_objeto")


class TestPlanificacion(unittest.TestCase):
    def setUp(self):
        self.nodo = util.modulo_nodo("protocolo")
        self.reglas = [
            [i, 4, 0, 1, 2, 1, 0, 0] for i in range(1, 31)
        ]
        self.hw = [
            {"pin": 2, "tipo": "digital_out"},
            {"pin": 4, "tipo": "digital_in"},
        ]

    def test_reparte_en_varios_lotes(self):
        cuerpos, info = host.planificar(
            self.reglas, mac=MAC, hw=self.hw, clave=CLAVE
        )
        self.assertGreater(info["lotes"], 1)
        self.assertEqual(info["reglas"], 30)
        self.assertEqual(cuerpos[0]["cmd"], "tx_begin")
        self.assertEqual(cuerpos[0]["t"], info["lotes"])
        self.assertEqual(cuerpos[0]["hw"], self.hw)
        self.assertEqual(cuerpos[-1]["cmd"], "tx_commit")

        for cuerpo in cuerpos:
            firmado = host.firmar(CLAVE, MAC, 99999999999, cuerpo)
            self.assertLessEqual(host.tamano(firmado), host.MAX_TRAMA)
            self.assertGreater(host.tamano(firmado), 0)

    def test_el_nodo_reconstruye_las_reglas(self):
        cuerpos, info = host.planificar(
            self.reglas, mac=MAC, hw=self.hw, clave=CLAVE
        )

        gestor = self.nodo.GestorTransacciones()
        inicio = cuerpos[0]
        gestor.iniciar(inicio["id"], inicio["t"], inicio.get("hw"), 0)
        for cuerpo in cuerpos[1:-1]:
            gestor.agregar(cuerpo["id"], cuerpo["k"], cuerpo["r"])
        reglas, hw = gestor.confirmar(cuerpos[-1]["id"])

        self.assertEqual(reglas, self.reglas)
        self.assertEqual(hw, self.hw)
        self.assertFalse(gestor.activa())

    def test_configuracion_vacia_borra_reglas(self):
        cuerpos, info = host.planificar([], mac=MAC, clave=CLAVE)
        self.assertEqual(info["lotes"], 0)
        self.assertEqual([c["cmd"] for c in cuerpos], ["tx_begin", "tx_commit"])
        gestor = self.nodo.GestorTransacciones()
        gestor.iniciar(cuerpos[0]["id"], cuerpos[0]["t"], None, 0)
        reglas, hw = gestor.confirmar(cuerpos[-1]["id"])
        self.assertEqual(reglas, [])

    def test_regla_gigante_no_cabe(self):
        gigante = [1, "P" * 400, 0, 1, 2, 1, 0, 0]
        with self.assertRaises(host.ErrorProtocolo):
            host.planificar([gigante], mac=MAC, clave=CLAVE)

    def test_rechaza_reglas_invalidas(self):
        with self.assertRaises(host.ErrorProtocolo):
            host.planificar([[1, 2, 3]], mac=MAC)
        with self.assertRaises(host.ErrorProtocolo):
            host.planificar([[1, 2, 9, 1, 2, 1, 0, 0]], mac=MAC)

    def test_demasiados_lotes(self):
        reglas = [[i, "P" * 120, 0, 1, 2, 1, 0, 0] for i in range(1, 20)]
        with self.assertRaises(host.ErrorProtocolo):
            host.planificar(reglas, mac=MAC, clave=CLAVE)


class TestGestorTransacciones(unittest.TestCase):
    def setUp(self):
        self.nodo = util.modulo_nodo("protocolo")
        self.gestor = self.nodo.GestorTransacciones()

    def test_lotes_idempotentes(self):
        self.gestor.iniciar(5, 2, [], 0)
        self.gestor.agregar(5, 0, [[1, 4, 0, 1, 2, 1, 0, 0]])
        self.gestor.agregar(5, 0, [[1, 4, 0, 1, 2, 1, 0, 0]])  # reintento
        self.gestor.agregar(5, 1, [[2, 4, 0, 1, 2, 1, 0, 0]])
        reglas, _ = self.gestor.confirmar(5)
        self.assertEqual(len(reglas), 2)

    def test_falta_un_lote(self):
        self.gestor.iniciar(5, 2, [], 0)
        self.gestor.agregar(5, 0, [[1, 4, 0, 1, 2, 1, 0, 0]])
        with self.assertRaises(self.nodo.ErrorProtocolo):
            self.gestor.confirmar(5)

    def test_transaccion_distinta(self):
        self.gestor.iniciar(5, 1, [], 0)
        with self.assertRaises(self.nodo.ErrorProtocolo):
            self.gestor.agregar(6, 0, [])

    def test_lote_fuera_de_rango(self):
        self.gestor.iniciar(5, 1, [], 0)
        with self.assertRaises(self.nodo.ErrorProtocolo):
            self.gestor.agregar(5, 3, [])

    def test_commit_repetido_es_idempotente(self):
        self.gestor.iniciar(5, 1, [], 0)
        self.gestor.agregar(5, 0, [[1, 4, 0, 1, 2, 1, 0, 0]])
        primera, _ = self.gestor.confirmar(5)
        segunda, _ = self.gestor.confirmar(5)
        self.assertEqual(primera, segunda)

    def test_abortar(self):
        self.gestor.iniciar(5, 1, [], 0)
        self.assertTrue(self.gestor.abortar(5))
        self.assertFalse(self.gestor.activa())

    def test_expira(self):
        self.gestor.iniciar(5, 1, [], 1000)
        self.assertFalse(self.gestor.expirada(1000 + self.nodo.VIGENCIA_TX_MS))
        self.assertTrue(self.gestor.expirada(1000 + self.nodo.VIGENCIA_TX_MS + 1))

    def test_valida_reglas_al_agregar(self):
        self.gestor.iniciar(5, 1, [], 0)
        with self.assertRaises(self.nodo.ErrorProtocolo):
            self.gestor.agregar(5, 0, [["no", "es", "una", "regla"]])


class TestUtilidades(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(host.a_ascii("Temperatura: 23 °C"), "Temperatura: 23 C")
        self.assertEqual(host.a_ascii("Año de la niña"), "Ano de la nina")
        self.assertEqual(host.a_ascii("¿Listo?"), "?Listo?")
        self.assertEqual(host.a_ascii(12), "12")

    def test_contador_monotono(self):
        contador = host.Contador()
        valores = [contador.siguiente() for _ in range(50)]
        self.assertEqual(valores, sorted(valores))
        self.assertEqual(len(set(valores)), len(valores))

    def test_version_y_limites(self):
        self.assertEqual(host.VERSION, 2)
        self.assertLessEqual(host.MAX_TRAMA, 250)
        self.assertEqual(host.MAX_TRAMA, util.modulo_nodo("protocolo").MAX_TRAMA)


if __name__ == "__main__":
    unittest.main()
