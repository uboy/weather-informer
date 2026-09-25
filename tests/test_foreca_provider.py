#!/usr/bin/env python3
"""Векторы ForecaProvider: конвертация periods -> формат информера, иконки по symbol,
48ч-фильтр, честные null (humidity/pressure), observations-парсер."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foreca_provider import ForecaProvider, ForecaError


def make_provider():
    return ForecaProvider(token="TEST", logger=None,
                          reverse_fn=lambda la, lo: {"name": "NN"},
                          geocode_fn=lambda q: [{"name": q, "lat": 56.32, "lon": 44.0}])


def make_periods(hours=49, start_delta_h=0, symbol="d200"):
    base = datetime.now() + timedelta(hours=start_delta_h)
    out = []
    for i in range(hours):
        d = base + timedelta(hours=i)
        out.append({
            "time": d.strftime("%Y-%m-%dT%H:%M:%S") + "+03:00",
            "temperature": 10 + (i % 9),
            "windSpeed": 3 + (i % 3),
            "windDir": (100 + i) % 360,
            "precipProb": i % 30,
            "precipAccum": 0,
            "symbol": symbol,
        })
    return out


class ForecaConvertTests(unittest.TestCase):
    def test_basic_convert(self):
        p = make_provider()
        d = p._convert(make_periods(), {"id": 1, "name": "NN", "tz": "Europe/Moscow"})
        self.assertEqual(d["src"], "Foreca")
        self.assertIsNotNone(d["fact"]["temp"])
        self.assertIsNone(d["fact"]["humidity"])   # API прогноз их не отдаёт — честный null
        self.assertIsNone(d["fact"]["pressure_mm"])
        self.assertTrue(d["forecasts"][0]["hours"])
        for name in ("night", "morning", "day", "evening"):
            self.assertIn(name, d["forecasts"][0]["parts"])

    def test_48h_filter(self):
        p = make_provider()
        d = p._convert(make_periods(hours=100), {"id": 1, "tz": "Europe/Moscow"})
        self.assertLessEqual(len(d["foreca_points"]), 50)

    def test_hours_from_current_hour(self):
        p = make_provider()
        d = p._convert(make_periods(), {"id": 1, "tz": "Europe/Moscow"})
        cur = datetime.now().hour
        hours = [int(h["hour"]) for h in d["forecasts"][0]["hours"]]
        self.assertTrue(all(h >= cur for h in hours), f"часы до текущего: {hours} vs {cur}")

    def test_icon_map(self):
        p = make_provider()
        cases = {"d000": "skc_d", "n000": "skc_n", "d100": "bkn_d", "n200": "bkn_n",
                 "d300": "ovc", "d400": "ovc", "d410": "ovc_ra", "d430": "ovc_ra",
                 "d500": "ovc_ra", "n600": "ovc_sn", "d730": "ovc_ts"}
        for sym, expected in cases.items():
            got = p._icon_from_point({"symbol": sym, "precip_prob": 0, "precip_mm": 0}, 12)
            self.assertEqual(got, expected, f"{sym} -> {got}, ожидалось {expected}")

    def test_precip_prob_override(self):
        p = make_provider()
        got = p._icon_from_point({"symbol": "d200", "precip_prob": 60, "precip_mm": 0}, 12)
        self.assertEqual(got, "ovc_ra")

    def test_empty_periods_raises(self):
        p = make_provider()
        with self.assertRaises(ForecaError):
            p._convert([], {"id": 1, "tz": "Europe/Moscow"})

    def test_observations_parse(self):
        p = make_provider()
        def route(url, timeout):
            if "/location/search/" in url:
                return '{"locations":[{"id":7,"name":"NN","timezone":"Europe/Moscow"}]}'.encode()
            return ('{"observations": ['
                    '{"station":"S1","time":"2026-09-25T12:00:00+03:00","temperature":16.7,'
                    '"relHumidity":66,"pressure":1024.4,"precip1h":[{"accum":0.4}]}'
                    ']}').encode()
        p._http_get = route
        obs = p.get_observations(latitude=56.32, longitude=44.0, stations=1)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["precip_mm"], 0.4)
        self.assertEqual(obs[0]["temperature"], 16.7)

    def test_location_search_cache(self):
        p = make_provider()
        calls = []
        p._http_get = lambda url, timeout: calls.append(url) or (
            '{"locations":[{"id":42,"name":"NN","timezone":"Europe/Moscow"}]}').encode()
        r1 = p._search_location("NN")
        r2 = p._search_location("NN")
        self.assertEqual(r1["id"], 42)
        self.assertEqual(r2["id"], 42)
        self.assertEqual(len(calls), 1, "кэш локейшена должен предотвращать повторный поиск")


if __name__ == "__main__":
    unittest.main(verbosity=2)
