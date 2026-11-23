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

    def test_backfill_from_observations(self):
        # Источник бэкфилла - observations (честные измерения станций):
        # давление конвертируется (metar-awc уже мм, foreca-obs - гПа),
        # повторный вызов не дублирует; forecast_points не читается вовсе,
        # прогнозная часть суток (valid_at=created) в историю не попадает
        # (регрессия дефекта «пилы», найденного владельцем 23.11).
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM forecast_points")
        conn.execute("DELETE FROM weather_fact_history")
        now = "2026-11-23T12:00:00"
        obs_rows = [
            (now, "Nizhny Novgorod/Strigino", "foreca-obs",
             5.0, 60.0, 1000.0, None, "STRIGINO", 4.0),
            ("2026-11-23T12:30:00", "UWGG", "metar-awc",
             4.0, 61.0, 744.0, None, "UWGG", 5.1),
        ]
        conn.executemany(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station, wind_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)", obs_rows)
        fp_rows = [
            (now, "Gismeteo", "56.3:44.0", now, 20.0, 65.0, 745.0, None, None),
        ]
        conn.executemany(
            "INSERT INTO forecast_points (created, provider, loc, valid_at, "
            "temperature, humidity, pressure, precip_prob, precip_mm) "
            "VALUES (?,?,?,?,?,?,?,?,?)", fp_rows)
        conn.commit()
        cs.backfill_fact_history()
        cs.backfill_fact_history()  # второй прогон: 0 новых строк
        n = conn.execute(
            "SELECT COUNT(*) FROM weather_fact_history").fetchone()[0]
        rows = conn.execute(
            "SELECT src, pressure_mm, temperature, wind_ms "
            "FROM weather_fact_history ORDER BY ts").fetchall()
        conn.close()
        self.assertEqual(n, 2)      # observations; forecast_points игнор
        # П7: foreca-obs разделяется по станции (phys_station STRIGINO)
        self.assertEqual([r[0] for r in rows],
                         ["foreca-obs:STRIGINO", "metar-awc"])
        self.assertAlmostEqual(rows[0][1], 750.062, places=2)  # гПа -> мм
        self.assertAlmostEqual(rows[1][1], 744.0, places=3)    # уже мм
        self.assertEqual(rows[0][2], 5.0)
        self.assertAlmostEqual(rows[0][3], 4.0)  # П7: ветер из observations
        self.assertAlmostEqual(rows[1][3], 5.1)

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
        # журнал добавляет счётчик ok_journal источнику
        self.assertEqual(j["sources"].get("Yandex", {}).get("ok_journal"), 1)
        kinds = [e["kind"] for e in j["events"]]
        self.assertIn("src_switch", kinds)
        self.assertIn("chain_fail", kinds)

    def test_sources_base_from_history_rows(self):
        # Источники панели строятся из истории фактов даже при пустом журнале
        # (переживает рестарт и кольцо 4000).
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM weather_fact_history")
        conn.commit()
        conn.close()
        cs._WEATHER_JOURNAL.clear()
        cs.record_fact_history("HistSrc", "loc1", {"temp": 5.0})
        st, raw = self.get("/api/weather/stats?days=7")
        self.assertEqual(st, 200)
        j = json.loads(raw)
        hs = j["sources"].get("HistSrc")
        self.assertIsNotNone(hs, "источник из истории не найден: %r" % j["sources"])
        self.assertEqual(hs["points"], 1)
        self.assertIsNotNone(hs["last_ts"])

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


    def test_synop_wind_decode(self):
        # П7: ветер Nddff из живых сообщений станции 27459 (synop_raw 23.11):
        # '81701'/'81801' непосредственно перед 1snTTT -> 1 м/с.
        dec = cs._decode_synop(
            "AAXX 23151 27459 21457 81701 11001 21023 39892 40088 52006 "
            "77172 887// 333 10010 69902 91003 90730 91104==")
        self.assertAlmostEqual(dec["wind_ms"], 1.0)
        self.assertAlmostEqual(dec["temperature"], -0.1)
        self.assertAlmostEqual(dec["pressure"], 989.2)
        dec2 = cs._decode_synop(
            "AAXX 23061 27459 42570 81801 11004 21028 39879 40075 52005 "
            "885// 333 91004 90760 91104==")
        self.assertAlmostEqual(dec2["wind_ms"], 1.0)

    def test_synop_wind_edge_cases(self):
        # N0hVVh ('50007' - видимость, ветра нет) -> NULL; спокойный '00000'
        # -> 0.0; только iRixhVV перед температурой ('41570', ff=70>36) -> NULL.
        dec = cs._decode_synop(
            "AAXX 23121 27459 41570 50007 10005 21032 39886 40081 52001 "
            "70272 86500 333 91003 90760 91104==")
        self.assertIsNone(dec["wind_ms"])
        dec2 = cs._decode_synop(
            "AAXX 23121 27459 41570 00000 10005 21032 39886 40081 52001 "
            "70272 86500 333 91003 90760 91104==")
        self.assertEqual(dec2["wind_ms"], 0.0)
        dec3 = cs._decode_synop(
            "AAXX 23121 27459 41570 10005 21032 39886 40081 52001 "
            "70272 86500 333 91003 90760 91104==")
        self.assertIsNone(dec3["wind_ms"])

    def test_metar_wind_units(self):
        # wspd у AWC API - узлы (17001MPS = 1 м/с -> wspd 2 kt): 2 kt -> 1.0.
        self.assertAlmostEqual(cs._metar_wind_ms(2), 1.0)
        self.assertAlmostEqual(cs._metar_wind_ms(8), 4.1)
        self.assertIsNone(cs._metar_wind_ms(None))
        self.assertIsNone(cs._metar_wind_ms("5"))  # не число -> NULL

    def test_station_split_migration(self):
        # П7: legacy foreca-obs переименовывается в foreca-obs:<phys_station>
        # при следующем подключении (миграция в _history_conn); идемпотентно.
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM weather_fact_history")
        conn.execute(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station) VALUES "
            "('2026-11-22T10:00:00', 'Nizhny Novgorod/Strigino', 'foreca-obs', "
            "5.0, 60.0, 1000.0, NULL, 'STRIGINO')")
        conn.execute(
            "INSERT INTO weather_fact_history (ts, src, loc, temperature) "
            "VALUES ('2026-11-22T10:00:00', 'foreca-obs', "
            "'Nizhny Novgorod/Strigino', 5.0)")
        conn.commit()
        conn.close()
        chk = cs._history_conn()
        chk.close()
        conn = sqlite3.connect(cs.FORECAST_DB)
        src = conn.execute("SELECT source FROM observations").fetchone()[0]
        fsrc = conn.execute("SELECT src FROM weather_fact_history").fetchone()[0]
        n_legacy = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'foreca-obs'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(src, "foreca-obs:STRIGINO")
        self.assertEqual(fsrc, "foreca-obs:STRIGINO")
        self.assertEqual(n_legacy, 0)

    def test_backfill_wind_synop_idempotent(self):
        # П7: ветер из synop_raw -> observations (только NULL-строки:
        # непустой wind_ms не перетирается; ts_local = UTC+3).
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS synop_raw "
                     "(ts_utc TEXT UNIQUE, raw TEXT)")
        conn.execute("DELETE FROM synop_raw")
        conn.execute("DELETE FROM observations")
        conn.execute(
            "INSERT INTO synop_raw VALUES ('2026-11-23T15:00:00Z', "
            "'AAXX 23151 27459 21457 81701 11001 21023 39892 40088 52006 "
            "77172 887// 333 10010 69902 91003 90730 91104==')")
        conn.execute(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station) VALUES "
            "('2026-11-23T18:00:00', 'Nizhny Novgorod WMO27459', "
            "'ogimet-synop', -0.1, NULL, 989.2, NULL, '27459')")
        conn.commit()
        conn.close()
        cs.backfill_wind_from_synop()
        conn = sqlite3.connect(cs.FORECAST_DB)
        w = conn.execute("SELECT wind_ms FROM observations").fetchone()[0]
        self.assertAlmostEqual(w, 1.0)
        conn.execute("UPDATE observations SET wind_ms = 9.9")
        conn.commit()
        conn.close()
        cs.backfill_wind_from_synop()
        conn = sqlite3.connect(cs.FORECAST_DB)
        w2 = conn.execute("SELECT wind_ms FROM observations").fetchone()[0]
        conn.close()
        self.assertAlmostEqual(w2, 9.9)


    def test_station_split_migration_clash(self):
        # П7 негатив: легаси fact-строка, чей переименованный двойник уже
        # существует, УДАЛЯЕТСЯ (значения строк идентичны) - без DELETE
        # миграционный UPDATE ловит UNIQUE и роняет каждый вызов
        # _history_conn; легаси-наблюдение без phys_station переименовывается
        # по канонной карте OGIMET_PHYS.
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM weather_fact_history")
        conn.execute(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station) VALUES "
            "('2026-11-22T10:00:00', 'Nizhny Novgorod/Strigino', 'foreca-obs', "
            "5.0, 60.0, 1000.0, NULL, NULL)")
        conn.execute(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station) VALUES "
            "('2026-11-22T10:00:00', 'Zavolzhe st', 'foreca-obs', "
            "4.0, 61.0, 1000.0, NULL, NULL)")
        conn.execute(
            "INSERT INTO weather_fact_history (ts, src, loc, temperature) "
            "VALUES ('2026-11-22T10:00:00', 'foreca-obs:STRIGINO', "
            "'Nizhny Novgorod/Strigino', 5.0)")
        conn.execute(
            "INSERT INTO weather_fact_history (ts, src, loc, temperature) "
            "VALUES ('2026-11-22T10:00:00', 'foreca-obs', "
            "'Nizhny Novgorod/Strigino', 5.0)")
        conn.commit()
        conn.close()
        chk = cs._history_conn()
        chk.close()
        conn = sqlite3.connect(cs.FORECAST_DB)
        obs_rows = conn.execute(
            "SELECT source, phys_station FROM observations "
            "ORDER BY station").fetchall()
        rows = conn.execute(
            "SELECT src FROM weather_fact_history").fetchall()
        conn.close()
        self.assertEqual(obs_rows[0], ("foreca-obs:STRIGINO", "STRIGINO"))
        # станция вне OGIMET_PHYS: phys = имя станции (семантика коллектора)
        self.assertEqual(obs_rows[1], ("foreca-obs:Zavolzhe st", "Zavolzhe st"))
        # легаси-дубль удалён, двойник цел: ровно одна строка
        self.assertEqual([r[0] for r in rows], ["foreca-obs:STRIGINO"])


    def test_fact_wind_refresh(self):
        # П7: fact-строки, созданные ДО появления wind_ms в observations,
        # получают ветер подтяжкой UPDATE...FROM; NULL-ветер наблюдения
        # в факт не пишет.
        conn = sqlite3.connect(cs.FORECAST_DB)
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM weather_fact_history")
        conn.execute(
            "INSERT INTO observations (ts, station, source, temperature, "
            "humidity, pressure, precip_mm, phys_station, wind_ms) VALUES "
            "('2026-11-22T12:00:00', 'Nizhny Novgorod WMO27459', "
            "'ogimet-synop', -1.0, NULL, 990.0, NULL, '27459', 3.3)")
        conn.execute(
            "INSERT INTO weather_fact_history (ts, src, loc, wind_ms) "
            "VALUES ('2026-11-22T12:00:00', 'ogimet-synop', "
            "'Nizhny Novgorod WMO27459', NULL)")
        conn.commit()
        conn.close()
        cs.backfill_fact_history()
        conn = sqlite3.connect(cs.FORECAST_DB)
        w = conn.execute(
            "SELECT wind_ms FROM weather_fact_history WHERE src = "
            "'ogimet-synop'").fetchone()[0]
        conn.close()
        self.assertAlmostEqual(w, 3.3)


if __name__ == "__main__":
    unittest.main()
