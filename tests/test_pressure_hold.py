#!/usr/bin/env python3
"""
Unit tests for 6-hour barometric pressure sample-and-hold logic and
anti-fake norm protection in caching_server.py.
"""

import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caching_server


class TestPressureHoldAndAntiFake(unittest.TestCase):
    def setUp(self):
        caching_server.LAST_KNOWN_PRESSURE.clear()
        if os.path.exists(caching_server.PRESSURE_CACHE_FILE):
            try:
                os.remove(caching_server.PRESSURE_CACHE_FILE)
            except OSError:
                pass

    def tearDown(self):
        caching_server.LAST_KNOWN_PRESSURE.clear()
        if os.path.exists(caching_server.PRESSURE_CACHE_FILE):
            try:
                os.remove(caching_server.PRESSURE_CACHE_FILE)
            except OSError:
                pass

    def test_record_and_get_held_pressure_within_6h(self):
        """Давление, измеренное 3 часа назад, успешно удерживается."""
        t0 = time.time() - (3 * 3600)
        caching_server.record_known_pressure(56.3177, 43.9993, 764, src="Yandex", ts=t0)
        held = caching_server.get_held_pressure(56.3177, 43.9993)
        self.assertEqual(held, 764)

    def test_held_pressure_expires_after_6h(self):
        """Давление старше 6 часов сбрасывается (устарело)."""
        t0 = time.time() - (6 * 3600 + 60)
        caching_server.record_known_pressure(56.3177, 43.9993, 764, src="Yandex", ts=t0)
        held = caching_server.get_held_pressure(56.3177, 43.9993)
        self.assertIsNone(held)

    def test_normalize_holds_last_valid_pressure(self):
        """В промежуточный час (без замера станции) подставляется последнее валидное давление."""
        t0 = time.time() - (2 * 3600)
        caching_server.record_known_pressure(56.3177, 43.9993, 765, src="Yandex", ts=t0)

        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4, "humidity": 50}  # pressure_mm отсутствует
        }
        res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303)
        self.assertEqual(res["fact"]["pressure_mm"], 765)

    def test_no_def_pressure_mm_fake_substitution(self):
        """def_pressure_mm (климатическая норма 743) НИКОГДА не подставляется вместо факта."""
        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4}
        }
        with patch("caching_server.fetch_fallback_pressure", return_value=None):
            res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303)
            self.assertIsNone(res["fact"]["pressure_mm"])
            self.assertNotEqual(res["fact"]["pressure_mm"], 743)

    def test_fallback_called_when_stale_or_empty(self):
        """При отсутствии свежего замера вызывается фоллбэк и кэшируется."""
        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4}
        }
        with patch("caching_server.fetch_fallback_pressure", return_value=763) as mock_fb:
            res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303)
            self.assertEqual(res["fact"]["pressure_mm"], 763)
            mock_fb.assert_called_once()
            held = caching_server.get_held_pressure(56.317722, 43.999303)
            self.assertEqual(held, 763)

    def test_coords_isolation(self):
        """Замер одного города не подставляется в координаты другого города."""
        caching_server.record_known_pressure(56.32, 44.0, 764, src="Yandex")  # НН
        held_msk = caching_server.get_held_pressure(55.75, 37.62)
        self.assertIsNone(held_msk)

    def test_cache_persistence_roundtrip(self):
        """Сохранение и загрузка кэша давления на диск переживают перезапуск."""
        now = time.time()
        caching_server.record_known_pressure(56.32, 44.0, 762, src="Yandex", ts=now)
        caching_server.LAST_KNOWN_PRESSURE.clear()
        self.assertIsNone(caching_server.get_held_pressure(56.32, 44.0))

        caching_server.load_pressure_cache()
        self.assertEqual(caching_server.get_held_pressure(56.32, 44.0), 762)

    def test_save_cache_prunes_expired_and_limits_max_entries(self):
        """Автоматическая очистка протухших точек (>6ч) и ограничение до MAX_PRESSURE_ENTRIES."""
        now = time.time()
        # Добавляем 40 точек, из них 5 протухших
        for i in range(40):
            ts = now - (7 * 3600) if i < 5 else now - (i * 60)
            caching_server.LAST_KNOWN_PRESSURE[f"5{i}.0:40.0"] = {"p": 750, "ts": ts, "src": "test"}

        caching_server.save_pressure_cache()
        # В кэше должно остаться не более MAX_PRESSURE_ENTRIES (32), и ни одной протухшей
        self.assertLessEqual(len(caching_server.LAST_KNOWN_PRESSURE), caching_server.MAX_PRESSURE_ENTRIES)
        for entry in caching_server.LAST_KNOWN_PRESSURE.values():
            self.assertLessEqual(now - entry["ts"], caching_server.PRESSURE_HOLD_MAX_AGE_SEC)

    def test_normalize_with_allow_network_false_skips_external_fetch(self):
        """allow_network=False запрещает сетевые запросы при старте сервера."""
        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4}
        }
        with patch("caching_server.fetch_fallback_pressure") as mock_fb:
            res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303, allow_network=False)
            self.assertIsNone(res["fact"]["pressure_mm"])
            mock_fb.assert_not_called()

    def test_anomalous_pressure_sanitized_to_none(self):
        """Аномальные или нечисловые значения давления сбрасываются в None."""
        cases = [-10, 500, 950, "нечисло", None]
        for bad_p in cases:
            payload = {"fact": {"pressure_mm": bad_p}}
            with patch("caching_server.fetch_fallback_pressure", return_value=None):
                res = caching_server.normalize_weather_data(payload, lat=56.32, lon=44.0)
                self.assertIsNone(res["fact"]["pressure_mm"], f"Значение {bad_p} должно быть санировано в None")

    def test_fallback_respects_config_toggle(self):
        """fetch_fallback_pressure не делает вызов к Open-Meteo, если он отключен в конфиге."""
        with patch("caching_server.load_config", return_value={"enable_openmeteo_fallback": False}):
            with patch("caching_server.fetch_url_via") as mock_http:
                p = caching_server.fetch_fallback_pressure(56.32, 44.0)
                self.assertIsNone(p)
                mock_http.assert_not_called()

    def test_concurrent_save_pressure_cache(self):
        """Параллельная запись из 10 потоков не повреждает pressure_cache.json."""
        errors = []
        def worker(idx):
            try:
                for j in range(10):
                    caching_server.record_known_pressure(56.0 + (idx * 0.01), 44.0 + (j * 0.01), 760 + idx)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Ошибки при параллельной записи: {errors}")
        # Проверяем, что файл читается корректно и содержит валидный JSON
        caching_server.load_pressure_cache()
        self.assertGreater(len(caching_server.LAST_KNOWN_PRESSURE), 0)


if __name__ == "__main__":
    unittest.main()
