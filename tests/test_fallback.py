#!/usr/bin/env python3
"""
Unit tests for fallback hierarchy and retry policy in caching_server.py.
Verifies:
1. Fallback hierarchy order: Yandex -> Gismeteo -> Foreca -> Open-Meteo -> OWM -> 7timer -> wttr.in
2. 403 / 429 quota protection: standard cooldown (no fast probing/hammering)
3. Transient network error: fast retry probe interval (default 10 min)
4. Primary recovery: return to Yandex once available
"""

import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request
import time
import os
import sys

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caching_server


class TestFallbackHierarchyAndProbe(unittest.TestCase):
    def setUp(self):
        caching_server.LOCATION_CACHE.clear()
        caching_server.cached_data = None
        caching_server.last_fetch_time = 0
        caching_server.last_error_message = None

    def test_1_hierarchy_order(self):
        """Проверка строгого порядка цепочки фоллбеков при последовательном отказе"""
        calls = []

        def fake_yandex(*args, **kwargs):
            calls.append("yandex")
            raise urllib.error.URLError("DNS timeout")

        def fake_gismeteo(*args, **kwargs):
            calls.append("gismeteo")
            raise Exception("Gismeteo parse error")

        def fake_foreca(*args, **kwargs):
            calls.append("foreca")
            return {
                "fact": {"temp": 17, "humidity": 70, "wind_speed": 2},
                "forecasts": [{"parts": {}}],
                "src": "Foreca"
            }

        def fake_om(*args, **kwargs):
            calls.append("om")
            return {"fact": {"temp": 16}, "forecasts": [], "src": "Open-Meteo"}

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch.object(caching_server.FORECA, "get_weather", side_effect=fake_foreca):
                    with patch("caching_server.fetch_from_openmeteo", side_effect=fake_om):
                        with patch("caching_server.load_config", return_value={
                            "api": "test-key",
                            "foreca_api_key": "test-token",
                            "enable_gismeteo_fallback": True,
                            "enable_foreca_fallback": True,
                            "enable_openmeteo_fallback": True,
                            "cache_interval_minutes": 60,
                            "fallback_interval_minutes": 10,
                        }):
                            res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0)
                            self.assertEqual(calls, ["yandex", "gismeteo", "foreca"])
                            self.assertEqual(res.get("src"), "Foreca")

    def test_2_network_error_sets_short_probe_ttl(self):
        """Временный сетевой сбой Яндекса (URLError) дает укороченный TTL (10 мин) для быстрого возврата"""
        def fake_yandex(*args, **kwargs):
            raise urllib.error.URLError("Temporary failure in name resolution")

        def fake_gismeteo(*args, **kwargs):
            return {
                "fact": {"temp": 18, "humidity": 65, "wind_speed": 1},
                "forecasts": [{"parts": {}}],
                "src": "Gismeteo"
            }

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch("caching_server.load_config", return_value={
                    "api": "test-key",
                    "enable_gismeteo_fallback": True,
                    "cache_interval_minutes": 60,
                    "fallback_interval_minutes": 10,
                }):
                    key = caching_server.loc_key(56.32, 44.0)
                    res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                    self.assertEqual(res.get("src"), "Gismeteo")
                    entry = caching_server.LOCATION_CACHE.get(key)
                    self.assertIsNotNone(entry)
                    # TTL должен быть 10 минут (600 с), а не 60 минут
                    self.assertEqual(entry.get("ttl"), 10 * 60)

    def test_3_quota_403_does_not_hammer(self):
        """Ошибка квоты 403 не включает быстрый опрос — сохраняется полный интервал (60 мин)"""
        def fake_yandex(*args, **kwargs):
            raise urllib.error.HTTPError("https://api.weather.yandex.ru", 403, "Forbidden", {}, None)

        def fake_gismeteo(*args, **kwargs):
            return {
                "fact": {"temp": 18, "humidity": 65, "wind_speed": 1},
                "forecasts": [{"parts": {}}],
                "src": "Gismeteo"
            }

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch("caching_server.load_config", return_value={
                    "api": "test-key",
                    "enable_gismeteo_fallback": True,
                    "cache_interval_minutes": 60,
                    "fallback_interval_minutes": 10,
                }):
                    key = caching_server.loc_key(56.32, 44.0)
                    res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                    self.assertEqual(res.get("src"), "Gismeteo")
                    entry = caching_server.LOCATION_CACHE.get(key)
                    self.assertIsNotNone(entry)
                    # При 403 TTL обязан оставаться полным (60 минут = 3600 с)
                    self.assertEqual(entry.get("ttl"), 60 * 60)

    def test_4_primary_recovery_returns_to_yandex(self):
        """После истечения короткого probe TTL при восстановлении связи сервер возвращается на Яндекс"""
        yandex_ok_data = {
            "fact": {"temp": 19, "humidity": 60, "wind_speed": 2},
            "forecasts": [{"parts": {}}],
            "src": "Yandex"
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"fact":{"temp":19},"forecasts":[{"parts":{}}]}'
        mock_resp.__enter__.return_value = mock_resp

        key = caching_server.loc_key(56.32, 44.0)
        # Имитируем старый фоллбек-кэш 11 минут назад (превысил 10-мин probe TTL)
        caching_server.LOCATION_CACHE[key] = {
            "data": {"src": "Gismeteo", "fact": {"temp": 15}},
            "ts": time.time() - 660,
            "ttl": 600
        }

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            with patch("caching_server.load_config", return_value={
                "api": "test-key",
                "cache_interval_minutes": 60,
                "fallback_interval_minutes": 10,
            }):
                res = caching_server.get_weather_for(56.32, 44.0, force=False)
                self.assertEqual(res.get("src"), "Yandex")
                entry = caching_server.LOCATION_CACHE.get(key)
                self.assertEqual(entry.get("ttl"), 60 * 60)


if __name__ == "__main__":
    unittest.main()
