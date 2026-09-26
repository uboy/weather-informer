#!/usr/bin/env python3
"""Векторы SYNOP-декодера (_decode_synop): температура/давление/осадки/защита от ветряных групп.
Каждый баг прошлых раундов здесь как регрессионный вектор: шкала осадков (991-999=следы),
ветряная Nddff-группа секции 1 не осадки, sec3 max-температура не перезаписывает факт."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caching_server import _decode_synop


class SynopDecodeTests(unittest.TestCase):
    def test_temperature(self):
        # 1sTTT: знак в s, TTT десятые
        self.assertEqual(_decode_synop("AAXX 07121 27459 42670 10238")["temperature"], 23.8)
        self.assertEqual(_decode_synop("AAXX 07121 27459 42670 11144")["temperature"], -14.4)
        self.assertEqual(_decode_synop("AAXX 25091 27459 42/70 10225")["temperature"], 22.5)

    def test_pressure_wmo_hundreds_rule(self):
        # val < 5000 -> +1000 hPa; val >= 5000 -> как есть (WMO)
        self.assertEqual(_decode_synop("AAXX 1 27459 30038")["pressure"], 1003.8)
        self.assertEqual(_decode_synop("AAXX 1 27459 39996")["pressure"], 999.6)
        self.assertEqual(_decode_synop("AAXX 1 27459 40226")["pressure_sl"], 1022.6)
        self.assertEqual(_decode_synop("AAXX 1 27459 40177")["pressure_sl"], 1017.7)

    def test_precip_scale(self):
        # WMO 4019: 001-988 целые мм; 991-999 следы (v-990)/10; 990 = нет данных
        self.assertEqual(_decode_synop("AAXX 1 27459 333 60002")["precip_mm"], 0.0)
        self.assertEqual(_decode_synop("AAXX 1 27459 333 60150")["precip_mm"], 15.0)
        self.assertEqual(_decode_synop("AAXX 1 27459 333 69911")["precip_mm"], 0.1)
        self.assertEqual(_decode_synop("AAXX 1 27459 333 69999")["precip_mm"], 0.9)
        self.assertIsNone(_decode_synop("AAXX 1 27459 333 69900")["precip_mm"])

    def test_wind_group_not_precip(self):
        # 61103 в секции 1 — облачность 6 окт + ветер 110°/03 м/с, НЕ осадки
        msg = "AAXX 25121 27459 42/70 61103 10225 20090 30054 40238 57002 82031 333 91007"
        d = _decode_synop(msg)
        self.assertIsNone(d["precip_mm"], "ветряная Nddff-группа секции 1 не должна парситься как осадки")
        self.assertEqual(d["temperature"], 22.5)

    def test_section3_precip_decoded(self):
        # 6RRRtR после 333 — настоящие осадки
        msg = "AAXX 24031 27459 22970 51003 10136 30028 40217 333 60002"
        self.assertEqual(_decode_synop(msg)["precip_mm"], 0.0)

    def test_sec3_maxtemp_not_overwrite(self):
        # 333 1snTTxTTx (суточный максимум) не должен перезаписывать мгновенную температуру
        msg = "AAXX 1 27459 10151 333 10263"
        d = _decode_synop(msg)
        self.assertEqual(d["temperature"], 15.1)

    def test_sec3_snow_not_overwrite_pressure(self):
        # 333 4E'sss (снежный покров, например 40015 = 15 см снега) не должен перезаписывать pressure_sl
        msg = "AAXX 1 27459 40226 333 40015"
        d = _decode_synop(msg)
        self.assertEqual(d["pressure_sl"], 1022.6, "группа 4E'sss в секции 3 не должна парситься как давление")

    def test_real_message_27459(self):
        # реальное сообщение из synop_raw (24.09 09:00 UTC): t=17.6, p=1002.8, slp=1021.2
        msg = ("AAXX 24091 27459 42970 71202 10238 20103 30030 40212 52006 80002 "
               "333 91006 90730 91108")
        d = _decode_synop(msg)
        self.assertEqual(d["temperature"], 23.8)
        self.assertEqual(d["pressure"], 1003.0)
        self.assertEqual(d["pressure_sl"], 1021.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
