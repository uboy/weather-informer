#!/usr/bin/env python3
"""
Unit tests for 6-hour barometric pressure sample-and-hold logic and
anti-fake norm protection in caching_server.py.
"""

import os
import sys
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
        # Кэш пуст, замера нет, фоллбэк вернул None
        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4}
        }
        with patch("caching_server.fetch_fallback_pressure", return_value=None):
            res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303)
            self.assertIsNone(res["fact"]["pressure_mm"])
            self.assertNotEqual(res["fact"]["pressure_mm"], 743)

    def test_fallback_called_when_stale_or_empty(self):
        """При отсутствии свежего замера вызывается фоллбэк (Open-Meteo) и кэшируется."""
        payload = {
            "info": {"lat": 56.317722, "lon": 43.999303, "def_pressure_mm": 743},
            "fact": {"temp": 4}
        }
        with patch("caching_server.fetch_fallback_pressure", return_value=763) as mock_fb:
            res = caching_server.normalize_weather_data(payload, lat=56.317722, lon=43.999303)
            self.assertEqual(res["fact"]["pressure_mm"], 763)
            mock_fb.assert_called_once()
            # Проверяем, что результат фоллбэка записался в удержание
            held = caching_server.get_held_pressure(56.317722, 43.999303)
            self.assertEqual(held, 763)

    def test_coords_isolation(self):
        """Замер одного города не подставляется в координаты другого города."""
        caching_server.record_known_pressure(56.32, 44.0, 764, src="Yandex")  # НН
        # Москва (55.75, 37.62) не должна получить давление НН
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


if __name__ == "__main__":
    unittest.main()
