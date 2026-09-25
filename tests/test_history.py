#!/usr/bin/env python3
"""record_forecast (часы сегодня/завтра, отсев прошедших, lead 23-46 оживлён)
и серверный 7timer (init UTC -> локальный блок; регрессия tz-скоса)."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import caching_server as cs


class RecordForecastTests(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.mktemp(suffix=".db")
        self.orig = cs.FORECAST_DB
        cs.FORECAST_DB = self.tmp
        conn = cs._history_conn()
        conn.execute("DELETE FROM forecast_points")
        conn.commit()
        conn.close()

    def tearDown(self):
        import os
        cs.FORECAST_DB = self.orig
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def _rows(self):
        conn = cs._history_conn()
        rows = conn.execute("SELECT valid_at, temperature FROM forecast_points WHERE provider='T'").fetchall()
        conn.close()
        return rows

    def test_today_future_and_fact(self):
        now = datetime.now()
        data = {"fact": {"temp": 20},
                "forecasts": [{"hours": [{"hour": str((now.hour + j) % 24), "temp": 20 + j} for j in range(24)]},
                              {"parts": {}}]}
        cs.record_forecast("T", "k", data)
        rows = self._rows()
        self.assertTrue(any(v.endswith(f"T{now.hour:02d}:00:00") for v, t in rows), "факт-строка на текущий час")

    def test_past_hours_skipped(self):
        now = datetime.now()
        data = {"fact": {"temp": 1},
                "forecasts": [{"hours": [{"hour": str((now.hour - 2) % 24), "temp": 99}]}, {}]}
        cs.record_forecast("T", "k", data)
        temps = [t for _, t in self._rows()]
        self.assertNotIn(99, temps, "прошедший час не должен записываться")

    def test_tomorrow_hours_recorded(self):
        now = datetime.now()
        tomorrow = now.date() + timedelta(days=1)
        data = {"fact": {"temp": 2},
                "forecasts": [
                    {"hours": [{"hour": str(now.hour), "temp": 5}]},
                    {"hours": [{"hour": "12", "temp": 33}], "parts": {}}]}
        cs.record_forecast("T", "k", data)
        rows = dict(self._rows())
        self.assertIn(f"{tomorrow.isoformat()}T12:00:00", rows, "завтрашний час обязан записываться (lead 23-46)")
        self.assertEqual(rows[f"{tomorrow.isoformat()}T12:00:00"], 33)


class Server7timerTests(unittest.TestCase):
    def test_init_utc_parsed_as_local(self):
        # init = сейчас-3ч UTC; timepoint 3 = сейчас локально -> факт = 1003
        now_local = datetime.now()
        init_utc = datetime.utcnow() - timedelta(hours=3)
        init_str = init_utc.strftime("%Y%m%d%H")
        ds = []
        for i in range(1, 65):
            ds.append({"timepoint": i * 3, "cloudcover": 30, "prec_type": "none",
                       "temp2m": 1000 + i * 3, "rh2m": "50%",
                       "wind10m": {"direction": "SE", "speed": 2}})
        payload = {"dataseries": ds, "init": init_str}
        orig = cs.fetch_url_via
        cs.fetch_url_via = lambda url, proxy, timeout, headers=None: __import__("json").dumps(payload)
        try:
            d = cs.fetch_from_7timer(56.32, 44.0)
        finally:
            cs.fetch_url_via = orig
        self.assertEqual(d["fact"]["temp"], 1003,
                         f"факт должен быть из блока timepoint 3 (=сейчас); tz-скос дал бы 1006. init={init_str}, now={now_local}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
