#!/usr/bin/env python3
"""Тесты фазы 1 (статистика погоды, 23.11): /api/weather/stats + накопители.

Покрывает: состав ответа (current/hourly/daily/sources/events), кламп days
1..HISTORY_DAYS, tolerant-дозагрузку weather_journal.jsonl (битая строка
пропускается молча - негативный кейс падает по своей причине), запись факта
с NULL-полями (no fake data), идемпотентность backfill (fact-строки переносятся
один раз, non-fact не переносятся), дневные агрегаты на фикстуре с известными
значениями, отсутствие секретов в ответе (эндпоинт без Basic auth).
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import caching_server as cs


class TestWeatherStats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.orig_config_file = cs.CONFIG_FILE
        cls.orig_forecast_db = cs.FORECAST_DB
        cls.orig_journal_file = cs.WEATHER_JOURNAL_FILE
        cs.CONFIG_FILE = os.path.join(cls.test_dir, "config.json")
        cs.FORECAST_DB = os.path.join(cls.test_dir, "forecast.db")
        cs.WEATHER_JOURNAL_FILE = os.path.join(cls.test_dir, "weather_journal.jsonl")
        cfg = {
            "port": 0,
            "dashboard_user": "u1",
            "dashboard_password": "secret-pass-xyz",
            "cameras": {"enabled": False},
        }
        with open(cs.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        cs._WEATHER_JOURNAL.clear()
        cs._CAM_JOURNAL.clear()
        cs.cached_data = None
        cs.last_fetch_time = None
        # Инициализация схемы forecast.db во временной БД (таблицы создаются
        # в _history_conn при первом подключении).
        _init = cs._history_conn()
        _init.close()
        cls.server = cs.BoundedThreadingHTTPServer(("127.0.0.1", 0), cs.WeatherHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cs.CONFIG_FILE = cls.orig_config_file
        cs.FORECAST_DB = cls.orig_forecast_db
        cs.WEATHER_JOURNAL_FILE = cls.orig_journal_file
        cs._WEATHER_JOURNAL.clear()
        cs._CAM_JOURNAL.clear()
        cs.cached_data = None
        cs.last_fetch_time = None

    def get(self, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_stats_composition(self):
        st, raw = self.get("/api/weather/stats?days=7")
        self.assertEqual(st, 200)
        j = json.loads(raw)
        for key in ("current", "hourly", "daily", "sources", "events", "days", "ts"):
            self.assertIn(key, j)
        self.assertEqual(j["days"], 7)
        self.assertIsInstance(j["hourly"], list)
        self.assertIsInstance(j["daily"], list)

    def test_stats_days_clamp(self):
        st, raw = self.get("/api/weather/stats?days=99999")
        self.assertEqual(json.loads(raw)["days"], cs.HISTORY_DAYS)  # кламп сверху
        st, raw = self.get("/api/weather/stats?days=-5")
        self.assertEqual(json.loads(raw)["days"], 1)                # кламп снизу
        st, raw = self.get("/api/weather/stats?days=abc")
        self.assertEqual(json.loads(raw)["days"], 7)                # мусор -> дефолт

    def test_journal_load_skips_garbage(self):
        # Негативный кейс: битая строка пропускается молча, хорошая - грузится.
        path = cs.WEATHER_JOURNAL_FILE
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "kind": "fetch_ok",
                                "source": "Yandex"}) + "\n")
            f.write("garbage line {{{\n")
            f.write(json.dumps({"ts": "not-int", "kind": "x"}) + "\n")
        cs._WEATHER_JOURNAL.clear()
        cs.weather_journal_load()
        good = [r for r in cs._WEATHER_JOURNAL if r.get("kind") == "fetch_ok"]
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0].get("source"), "Yandex")
        self.assertEqual(len(cs._WEATHER_JOURNAL), 1)  # битые строки не в буфере

    def test_record_fact_history_null_fields(self):
        # Источник без давления/ветра: поля NULL, строка записана (no fake data).
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM weather_fact_history")
        conn.commit()
        conn.close()
        cs.record_fact_history("TestSrc", "loc1", {"temp": -3.5, "humidity": 71})
        conn = sqlite3.connect(cs.FORECAST_DB)
        rows = conn.execute(
            "SELECT src, temperature, humidity, pressure_mm, wind_ms "
            "FROM weather_fact_history WHERE src = 'TestSrc'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "TestSrc")
        self.assertAlmostEqual(rows[0][1], -3.5)
        self.assertEqual(rows[0][2], 71)
        self.assertIsNone(rows[0][3])  # pressure_mm не передан -> NULL
        self.assertIsNone(rows[0][4])  # wind_ms не передан -> NULL
        # не-дикт вообще не пишет
        cs.record_fact_history("TestSrc", "loc1", "not-a-dict")
        conn = sqlite3.connect(cs.FORECAST_DB)
        n = conn.execute("SELECT COUNT(*) FROM weather_fact_history "
                         "WHERE src = 'TestSrc'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_backfill_idempotent(self):
        # fact-строки (valid_at = created) переносятся, non-fact (valid_at !=
        # created) - нет; повторный вызов не дублирует.
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM forecast_points")
        conn.execute("DELETE FROM weather_fact_history")
        now = "2026-11-23T12:00:00"
        rows = [
            # fact-строки: переносятся
            (now, "Yandex", "56.3:44.0", now, 5.0, 60.0, 745.0, None, None),
            (now, "Gismeteo", "56.3:44.0", now, 4.0, 61.0, 744.0, None, None),
            # почасовой прогноз: НЕ переносится
            (now, "Yandex", "56.3:44.0", "2026-11-23T13:00:00", 6.0, 62.0, 746.0, None, None),
        ]
        conn.executemany(
            "INSERT INTO forecast_points (created, provider, loc, valid_at, "
            "temperature, humidity, pressure, precip_prob, precip_mm) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        cs.backfill_fact_history()
        cs.backfill_fact_history()  # второй прогон: 0 новых строк
        n = conn.execute(
            "SELECT COUNT(*) FROM weather_fact_history").fetchone()[0]
        temps = sorted(r[0] for r in conn.execute(
            "SELECT temperature FROM weather_fact_history").fetchall())
        winds = conn.execute(
            "SELECT COUNT(*) FROM weather_fact_history "
            "WHERE wind_ms IS NOT NULL").fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)          # только fact-строки
        self.assertEqual(temps, [4.0, 5.0])
        self.assertEqual(winds, 0)      # wind в forecast_points нет -> NULL

    def test_daily_aggregates_on_fixture(self):
        # Три факта за сегодня с разными ts (в одну секунду UNIQUE(ts,src,loc)
        # схлопнул бы их - это корректно, поэтому ts фиксированные): tmin 10,
        # tavg 20, tmax 30.
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM weather_fact_history")
        today = time.strftime("%Y-%m-%d")
        rows = [("AggSrc", "12:00:00", 10.0), ("AggSrc", "13:00:00", 20.0),
                ("AggSrc", "14:00:00", 30.0)]
        conn.executemany(
            "INSERT INTO weather_fact_history (ts, src, loc, temperature) "
            "VALUES (?,?,?,?)",
            [(today + "T" + hms, src, "loc1", t) for src, hms, t in rows])
        conn.commit()
        conn.close()
        st, raw = self.get("/api/weather/stats?days=30")
        self.assertEqual(st, 200)
        j = json.loads(raw)
        days = [d for d in j["daily"] if d["tmin"] == 10.0]
        self.assertTrue(days, "ожидаемая дневная строка не найдена: %r" % j["daily"])
        d = days[-1]
        self.assertEqual(d["tmin"], 10.0)
        self.assertEqual(d["tmax"], 30.0)
        self.assertEqual(d["tavg"], 20.0)
        self.assertEqual(d["n"], 3)
        # hourly содержит точки источника AggSrc
        self.assertTrue(any(h["src"] == "AggSrc" for h in j["hourly"]))

    def test_sources_and_events_from_journal(self):
        cs._WEATHER_JOURNAL.clear()
        cs.weather_journal_record("fetch_ok", source="Yandex")
        cs.weather_journal_record("src_switch", source="Gismeteo",
                                  prev_source="Yandex", note="quota")
        cs.weather_journal_record("chain_fail", error="all sources failed",
                                  quota_blocked=False)
        st, raw = self.get("/api/weather/stats?days=1")
        self.assertEqual(st, 200)
        j = json.loads(raw)
        self.assertEqual(j["sources"].get("Yandex", {}).get("ok"), 1)
        kinds = [e["kind"] for e in j["events"]]
        self.assertIn("src_switch", kinds)
        self.assertIn("chain_fail", kinds)

    def test_no_secrets_in_response(self):
        cs.cached_data = {"src": "Yandex", "city_name": "НН",
                          "fact": {"temp": 1.0, "humidity": 50,
                                   "pressure_mm": 748, "wind_speed": 2.0}}
        cs.last_fetch_time = time.time()
        cs._WEATHER_JOURNAL.clear()
        cs.weather_journal_record("fetch_ok", source="Yandex")
        try:
            st, raw = self.get("/api/weather/stats?days=7")
            self.assertEqual(st, 200)
            self.assertNotIn("secret-pass-xyz", raw)   # пароль дашборда
            self.assertNotIn("dashboard_password", raw)
            self.assertNotIn("tablet_token", raw)
            j = json.loads(raw)
            self.assertEqual(j["current"]["temp"], 1.0)
            self.assertEqual(j["current"]["src"], "Yandex")
        finally:
            cs.cached_data = None
            cs.last_fetch_time = None


if __name__ == "__main__":
    unittest.main()
