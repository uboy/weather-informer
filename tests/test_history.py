#!/usr/bin/env python3
"""record_forecast (часы сегодня/завтра, отсев прошедших, lead 23-46 оживлён)
и серверный 7timer (init UTC -> локальный блок; регрессия tz-скоса)."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import caching_server as cs


from unittest.mock import patch


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

    @patch.object(cs, "datetime")
    def test_past_hours_skipped(self, mock_dt):
        fixed_now = datetime(2026, 9, 26, 14, 0, 0)
        mock_dt.now.return_value = fixed_now
        data = {"fact": {"temp": 1},
                "forecasts": [{"hours": [{"hour": "10", "temp": 99}, {"hour": "15", "temp": 25}]}, {}]}
        cs.record_forecast("T", "k", data)
        temps = [t for _, t in self._rows()]
        self.assertNotIn(99, temps, "прошедший час 10 не должен записываться при cur_hour=14")
        self.assertIn(25, temps, "будущий час 15 должен быть записан")

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

    def test_tomorrow_parts_recorded_when_hours_missing(self):
        now = datetime.now()
        tomorrow = now.date() + timedelta(days=1)
        data = {"fact": {"temp": 2},
                "forecasts": [
                    {"hours": [{"hour": str(now.hour), "temp": 5}]},
                    {"parts": {"morning": {"temp_avg": 14}, "day": {"temp_avg": 18}}}]}
        cs.record_forecast("T", "k", data)
        rows = dict(self._rows())
        self.assertIn(f"{tomorrow.isoformat()}T08:00:00", rows, "завтрашнее утро (08:00) из parts должно записаться")
        self.assertEqual(rows[f"{tomorrow.isoformat()}T08:00:00"], 14)
        self.assertIn(f"{tomorrow.isoformat()}T14:00:00", rows, "завтрашний день (14:00) из parts должен записаться")
        self.assertEqual(rows[f"{tomorrow.isoformat()}T14:00:00"], 18)


class OpenMeteoServerTests(unittest.TestCase):
    def test_part_wind_date_filter_and_vector_dir(self):
        # 48 часов: сегодня и завтра. Завтра — ураган 50 м/с и ветер 180.
        # Сегодня с 12 до 18: ветер 5 м/с и направления 350 и 10 градусов -> средний угол 0.
        times = []
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        for h in range(24):
            times.append(f"{today.isoformat()}T{h:02d}:00")
        for h in range(24):
            times.append(f"{tomorrow.isoformat()}T{h:02d}:00")

        ws = [0.0] * 48
        wd = [0] * 48
        # Сегодня с 12 до 18: ветер 18 km/h (5 m/s), чередуем 350 и 10 градусов
        for h in range(12, 18):
            ws[h] = 18.0
            wd[h] = 350 if h % 2 == 0 else 10
        # Завтра в то же время ураган 180 km/h (50 m/s) и направление 180
        for h in range(24 + 12, 24 + 18):
            ws[h] = 180.0
            wd[h] = 180

        om_payload = {
            "current_weather": {"temperature": 15, "windspeed": 18, "winddirection": 0, "weathercode": 0, "time": times[12]},
            "hourly": {
                "time": times,
                "temperature_2m": [15] * 48,
                "windspeed_10m": ws,
                "winddirection_10m": wd,
                "weathercode": [0] * 48,
                "relativehumidity_2m": [50] * 48,
                "surface_pressure": [1013] * 48
            },
            "daily": {
                "sunrise": [f"{today.isoformat()}T06:00"],
                "sunset": [f"{today.isoformat()}T18:00"],
                "temperature_2m_max": [20],
                "temperature_2m_min": [10]
            }
        }
        orig = cs.fetch_url_via
        cs.fetch_url_via = lambda url, proxy, timeout, headers=None: __import__("json").dumps(om_payload)
        try:
            res = cs.fetch_from_openmeteo(56.3, 44.0)
        finally:
            cs.fetch_url_via = orig
        day_part = res["forecasts"][0]["parts"]["day"]
        self.assertEqual(day_part["wind_speed"], 5.0, "ветер должен усредняться только за сегодня")
        self.assertEqual(day_part["wind_angle"], 0, "углы 350 и 10 должны усредняться в 0 (векторное среднее)")
        self.assertEqual(len(res["forecasts"][1]["hours"]), 24, "завтрашние 24 часа должны быть в forecasts[1].hours")


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
