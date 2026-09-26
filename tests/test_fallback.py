#!/usr/bin/env python3
"""
Unit tests for fallback hierarchy, fast-probing, and retry policy in caching_server.py.
Verifies:
1. Fallback hierarchy order: Yandex -> Gismeteo -> Foreca -> Open-Meteo -> OWM -> 7timer -> wttr.in
2. Yandex error categorization:
   - 401, 402, 403, 429 quota/auth protection: standard cooldown (60 min)
   - 500, 502, 503, 504 server errors: fast probe interval (15 min)
   - Network errors (URLError, socket.timeout, TimeoutError): fast probe interval (15 min)
   - Corrupted/incomplete JSON: fast probe interval (15 min) and fallback
3. Config toggles (enable_*_fallback=False) and missing API keys
4. Total failure handling (with old cache retention vs without prior cache, negative cache fail_ttl)
5. Cache eviction policy (preserving default city and active key up to MAX_LOCATIONS)
6. Source parameter isolation (?source=yandex, ?source=gismeteo, etc.)
7. Disk cache restoration TTL (Yandex 60 min vs fallback 15 min)
8. Primary recovery: return to Yandex once available
"""

import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request
import socket
import json
import time
import os
import sys
import tempfile

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caching_server


class TestFallbackHierarchyAndProbe(unittest.TestCase):
    def setUp(self):
        caching_server.LOCATION_CACHE.clear()
        caching_server.KEY_LOCKS.clear()
        caching_server.cached_data = None
        caching_server.last_fetch_time = 0
        caching_server.last_error_message = None
        self._save_disk_patcher = patch("caching_server.save_disk_cache")
        self._save_disk_patcher.start()

    def tearDown(self):
        try:
            self._save_disk_patcher.stop()
        except Exception:
            pass
        caching_server.LOCATION_CACHE.clear()
        caching_server.KEY_LOCKS.clear()
        caching_server.cached_data = None
        caching_server.last_fetch_time = 0
        caching_server.last_error_message = None

    def test_1_hierarchy_order_foreca(self):
        """Проверка строгого порядка цепочки: отказ Яндекса и Gismeteo -> ответ Foreca"""
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

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch.object(caching_server.FORECA, "get_weather", side_effect=fake_foreca):
                    with patch("caching_server.load_config", return_value={
                        "api": "test-key",
                        "foreca_api_key": "test-token",
                        "enable_gismeteo_fallback": True,
                        "enable_foreca_fallback": True,
                        "cache_interval_minutes": 60,
                        "fallback_interval_minutes": 10,
                    }):
                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0)
                        self.assertEqual(calls, ["yandex", "gismeteo", "foreca"])
                        self.assertEqual(res.get("src"), "Foreca")

    def test_1_hierarchy_order_full_chain_to_wttr(self):
        """Проверка полного обхода всей цепочки вплоть до wttr.in при отказе всех предыдущих"""
        calls = []

        def fake_yandex(*args, **kwargs):
            calls.append("yandex")
            raise urllib.error.URLError("DNS timeout")

        def fake_gismeteo(*args, **kwargs):
            calls.append("gismeteo")
            raise Exception("Gismeteo error")

        def fake_foreca(*args, **kwargs):
            calls.append("foreca")
            raise Exception("Foreca error")

        def fake_om(*args, **kwargs):
            calls.append("om")
            raise Exception("Open-Meteo error")

        def fake_owm(*args, **kwargs):
            calls.append("owm")
            raise Exception("OWM error")

        def fake_7timer(*args, **kwargs):
            calls.append("7timer")
            raise Exception("7timer error")

        def fake_wttr(*args, **kwargs):
            calls.append("wttr")
            return {
                "fact": {"temp": 12, "humidity": 80, "wind_speed": 4},
                "forecasts": [{"parts": {}}],
                "src": "wttr.in"
            }

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch.object(caching_server.FORECA, "get_weather", side_effect=fake_foreca):
                    with patch("caching_server.fetch_from_openmeteo", side_effect=fake_om):
                        with patch("caching_server.fetch_from_openweathermap", side_effect=fake_owm):
                            with patch("caching_server.fetch_from_7timer", side_effect=fake_7timer):
                                with patch("caching_server.fetch_from_wttr", side_effect=fake_wttr):
                                    with patch("caching_server.load_config", return_value={
                                        "api": "test-key",
                                        "foreca_api_key": "test-token",
                                        "openweathermap_api_key": "owm-token",
                                        "enable_gismeteo_fallback": True,
                                        "enable_foreca_fallback": True,
                                        "enable_openmeteo_fallback": True,
                                        "enable_openweathermap_fallback": True,
                                        "enable_7timer_fallback": True,
                                        "enable_wttr_fallback": True,
                                        "cache_interval_minutes": 60,
                                        "fallback_interval_minutes": 15,
                                    }):
                                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0)
                                        self.assertEqual(calls, ["yandex", "gismeteo", "foreca", "om", "owm", "7timer", "wttr"])
                                        self.assertEqual(res.get("src"), "wttr.in")

    def test_2_network_error_sets_short_probe_ttl(self):
        """Временный сетевой сбой Яндекса (URLError) дает укороченный TTL (15 мин) для быстрого возврата"""
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
                    "fallback_interval_minutes": 15,
                }):
                    key = caching_server.loc_key(56.32, 44.0)
                    res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                    self.assertEqual(res.get("src"), "Gismeteo")
                    entry = caching_server.LOCATION_CACHE.get(key)
                    self.assertIsNotNone(entry)
                    # TTL должен быть 15 минут (900 с), а не 60 минут
                    self.assertEqual(entry.get("ttl"), 15 * 60)

    def test_3_quota_codes_keep_standard_cooldown(self):
        """Ошибки квоты и авторизации (401, 402, 403, 429) сохраняют полный cooldown (60 мин), не долбят API"""
        for code in (401, 402, 403, 429):
            caching_server.LOCATION_CACHE.clear()

            def fake_yandex(*args, **kwargs):
                raise urllib.error.HTTPError("https://api.weather.yandex.ru", code, f"Error {code}", {}, None)

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
                        "fallback_interval_minutes": 15,
                    }):
                        key = caching_server.loc_key(56.32, 44.0)
                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                        self.assertEqual(res.get("src"), "Gismeteo")
                        entry = caching_server.LOCATION_CACHE.get(key)
                        self.assertIsNotNone(entry)
                        self.assertEqual(entry.get("ttl"), 60 * 60, f"Code {code} must keep 60-min interval")

    def test_yandex_server_errors_5xx_give_probe_ttl(self):
        """Ошибки сервера Яндекса (500, 502, 503, 504) считаются временными и выставляют fast-probe TTL 15 мин"""
        for code in (500, 502, 503, 504):
            caching_server.LOCATION_CACHE.clear()

            def fake_yandex(*args, **kwargs):
                raise urllib.error.HTTPError("https://api.weather.yandex.ru", code, f"Gateway Error {code}", {}, None)

            def fake_gismeteo(*args, **kwargs):
                return {
                    "fact": {"temp": 15, "humidity": 70, "wind_speed": 3},
                    "forecasts": [{"parts": {}}],
                    "src": "Gismeteo"
                }

            with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
                with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                    with patch("caching_server.load_config", return_value={
                        "api": "test-key",
                        "enable_gismeteo_fallback": True,
                        "cache_interval_minutes": 60,
                        "fallback_interval_minutes": 15,
                    }):
                        key = caching_server.loc_key(56.32, 44.0)
                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                        self.assertEqual(res.get("src"), "Gismeteo")
                        entry = caching_server.LOCATION_CACHE.get(key)
                        self.assertIsNotNone(entry)
                        self.assertEqual(entry.get("ttl"), 15 * 60, f"Code {code} must use 15-min probe TTL")

    def test_yandex_socket_and_timeout_errors(self):
        """Таймауты сокетов (socket.timeout, TimeoutError) дают probe TTL 15 мин"""
        for err in (socket.timeout("Socket timed out"), TimeoutError("Connection timed out")):
            caching_server.LOCATION_CACHE.clear()

            def fake_yandex(*args, **kwargs):
                raise err

            def fake_gismeteo(*args, **kwargs):
                return {
                    "fact": {"temp": 14, "humidity": 75, "wind_speed": 2},
                    "forecasts": [{"parts": {}}],
                    "src": "Gismeteo"
                }

            with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
                with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                    with patch("caching_server.load_config", return_value={
                        "api": "test-key",
                        "enable_gismeteo_fallback": True,
                        "cache_interval_minutes": 60,
                        "fallback_interval_minutes": 15,
                    }):
                        key = caching_server.loc_key(56.32, 44.0)
                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                        self.assertEqual(res.get("src"), "Gismeteo")
                        entry = caching_server.LOCATION_CACHE.get(key)
                        self.assertEqual(entry.get("ttl"), 15 * 60)

    def test_yandex_malformed_and_incomplete_payload(self):
        """Некорректный JSON или отсутствие fact/forecasts переводит на фоллбек с probe TTL 15 мин"""
        bad_responses = [
            b"Internal Server Error (Not JSON)",
            b"{}",
            b'{"fact": {"temp": 10}}',  # missing forecasts
            b'{"forecasts": []}',         # missing fact
            b"[]",                        # JSON list instead of object
        ]

        for payload in bad_responses:
            caching_server.LOCATION_CACHE.clear()
            mock_resp = MagicMock()
            mock_resp.read.return_value = payload
            mock_resp.__enter__.return_value = mock_resp

            def fake_gismeteo(*args, **kwargs):
                return {
                    "fact": {"temp": 16, "humidity": 70, "wind_speed": 2},
                    "forecasts": [{"parts": {}}],
                    "src": "Gismeteo"
                }

            with patch.object(urllib.request, "urlopen", return_value=mock_resp):
                with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                    with patch("caching_server.load_config", return_value={
                        "api": "test-key",
                        "enable_gismeteo_fallback": True,
                        "cache_interval_minutes": 60,
                        "fallback_interval_minutes": 15,
                    }):
                        key = caching_server.loc_key(56.32, 44.0)
                        res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0, key=key)
                        self.assertEqual(res.get("src"), "Gismeteo")
                        entry = caching_server.LOCATION_CACHE.get(key)
                        self.assertEqual(entry.get("ttl"), 15 * 60)

    def test_disabled_fallback_flags_and_missing_keys(self):
        """Отключение источников через флаги конфига или отсутствие API-ключей пропускает их"""
        calls = []

        def fake_yandex(*args, **kwargs):
            calls.append("yandex")
            raise urllib.error.URLError("DNS timeout")

        def fake_gismeteo(*args, **kwargs):
            calls.append("gismeteo")
            return {"fact": {"temp": 10}, "forecasts": [], "src": "Gismeteo"}

        def fake_foreca(*args, **kwargs):
            calls.append("foreca")
            return {"fact": {"temp": 11}, "forecasts": [], "src": "Foreca"}

        def fake_om(*args, **kwargs):
            calls.append("om")
            return {"fact": {"temp": 12}, "forecasts": [], "src": "Open-Meteo"}

        # Отключаем Gismeteo (флагом) и Foreca (пустым ключом) -> фетч должен пойти в Open-Meteo
        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch.object(caching_server.FORECA, "get_weather", side_effect=fake_foreca):
                    with patch("caching_server.fetch_from_openmeteo", side_effect=fake_om):
                        with patch("caching_server.load_config", return_value={
                            "api": "test-key",
                            "enable_gismeteo_fallback": False,  # отключен
                            "enable_foreca_fallback": True,
                            "foreca_api_key": "",              # ключ не задан
                            "enable_openmeteo_fallback": True,
                            "cache_interval_minutes": 60,
                            "fallback_interval_minutes": 15,
                        }):
                            res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0)
                            self.assertEqual(calls, ["yandex", "om"])
                            self.assertEqual(res.get("src"), "Open-Meteo")

    def test_total_failure_preserves_old_cache_and_sets_fail_ttl(self):
        """При тотальном сбое всех источников старый кэш сохраняется и получает fail_ttl"""
        key = caching_server.loc_key(56.32, 44.0)
        old_data = {"fact": {"temp": 22}, "forecasts": [], "src": "Gismeteo"}
        caching_server.LOCATION_CACHE[key] = {
            "data": old_data,
            "ts": time.time() - 3700,
            "ttl": 3600
        }

        def fail_all(*args, **kwargs):
            raise urllib.error.URLError("Network down")

        with patch.object(urllib.request, "urlopen", side_effect=fail_all):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fail_all):
                with patch.object(caching_server.FORECA, "get_weather", side_effect=fail_all):
                    with patch("caching_server.fetch_from_openmeteo", side_effect=fail_all):
                        with patch("caching_server.fetch_from_openweathermap", side_effect=fail_all):
                            with patch("caching_server.fetch_from_7timer", side_effect=fail_all):
                                with patch("caching_server.fetch_from_wttr", side_effect=fail_all):
                                    with patch("caching_server.load_config", return_value={
                                        "api": "test-key",
                                        "enable_gismeteo_fallback": True,
                                        "enable_foreca_fallback": True,
                                        "enable_openmeteo_fallback": True,
                                        "enable_openweathermap_fallback": True,
                                        "enable_7timer_fallback": True,
                                        "enable_wttr_fallback": True,
                                        "cache_interval_minutes": 60,
                                        "fallback_interval_minutes": 15,
                                    }):
                                        res = caching_server.get_weather_for(56.32, 44.0, force=False)
                                        # Должны вернуться старые сохраненные данные
                                        self.assertEqual(res, old_data)
                                        entry = caching_server.LOCATION_CACHE.get(key)
                                        self.assertIsNotNone(entry)
                                        self.assertEqual(entry.get("ttl"), 15 * 60)
                                        # Повторный запрос внутри fail_ttl не делает сетевых вызовов
                                        with patch.object(caching_server, "_fetch_weather_locked") as mock_fetch:
                                            res2 = caching_server.get_weather_for(56.32, 44.0, force=False)
                                            self.assertEqual(res2, old_data)
                                            mock_fetch.assert_not_called()

    def test_total_failure_without_prior_cache_respects_negative_cache_ttl(self):
        """При тотальном сбое без старых данных выставляется отрицательный кэш, защищающий от спама"""
        key = caching_server.loc_key(56.32, 44.0)

        def fail_all(*args, **kwargs):
            raise urllib.error.URLError("No connection")

        with patch.object(urllib.request, "urlopen", side_effect=fail_all):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fail_all):
                with patch("caching_server.load_config", return_value={
                    "api": "test-key",
                    "enable_gismeteo_fallback": True,
                    "enable_foreca_fallback": False,
                    "enable_openmeteo_fallback": False,
                    "enable_openweathermap_fallback": False,
                    "enable_7timer_fallback": False,
                    "enable_wttr_fallback": False,
                    "cache_interval_minutes": 60,
                    "fallback_interval_minutes": 15,
                }):
                    res = caching_server.get_weather_for(56.32, 44.0, force=False)
                    self.assertIsNone(res)
                    entry = caching_server.LOCATION_CACHE.get(key)
                    self.assertIsNotNone(entry)
                    self.assertIsNone(entry.get("data"))
                    self.assertEqual(entry.get("ttl"), 15 * 60)

                    # Повторный вызов не должен перезапускать _fetch_weather_locked
                    with patch.object(caching_server, "_fetch_weather_locked") as mock_fetch:
                        res2 = caching_server.get_weather_for(56.32, 44.0, force=False)
                        self.assertIsNone(res2)
                        mock_fetch.assert_not_called()

    def test_cache_eviction_protects_default_location_and_handles_safe_ts(self):
        """Эвикция кэша при превышении MAX_LOCATIONS не удаляет дефолтный город и не падает без ts"""
        cfg = {"lat": 56.317722, "lon": 43.999303, "cache_interval_minutes": 60}
        defk = caching_server.loc_key(cfg["lat"], cfg["lon"])

        with patch("caching_server.load_config", return_value=cfg):
            # Заполняем кэш 10 точками (> MAX_LOCATIONS = 8)
            now = time.time()
            caching_server.LOCATION_CACHE[defk] = {"data": {"src": "Yandex"}, "ts": now - 500, "ttl": 3600}
            for i in range(1, 10):
                k = f"city_{i}:lon"
                caching_server.LOCATION_CACHE[k] = {"data": {"src": "OM"}, "ts": now - 1000 + i * 10, "ttl": 3600}

            # Добавляем 11-ю точку через _store_location_result
            new_key = "city_new:lon"
            caching_server._store_location_result(new_key, {"src": "Foreca"}, ttl=900)

            # Проверяем размер кэша и сохранность ключевых элементов
            self.assertLessEqual(len(caching_server.LOCATION_CACHE), caching_server.MAX_LOCATIONS)
            self.assertIn(defk, caching_server.LOCATION_CACHE, "Дефолтный город никогда не должен эвиктиться")
            self.assertIn(new_key, caching_server.LOCATION_CACHE, "Только что записанный ключ должен остаться")

    def test_source_parameter_query_isolation(self):
        """Параметр sources изолирует фетч: сбой ?source=yandex не трогает фоллбеки и другие источники"""
        yandex_called = []
        gismeteo_called = []

        def fake_yandex(*args, **kwargs):
            yandex_called.append(True)
            raise urllib.error.URLError("Network down")

        def fake_gismeteo(*args, **kwargs):
            gismeteo_called.append(True)
            return {"fact": {"temp": 20}, "forecasts": [], "src": "Gismeteo"}

        with patch.object(urllib.request, "urlopen", side_effect=fake_yandex):
            with patch.object(caching_server.GISMETEO, "get_weather", side_effect=fake_gismeteo):
                with patch("caching_server.load_config", return_value={
                    "api": "test-key",
                    "enable_gismeteo_fallback": True,
                    "cache_interval_minutes": 60,
                    "fallback_interval_minutes": 15,
                }):
                    # Запрос конкретно ?source=yandex
                    res = caching_server.get_weather_for(56.32, 44.0, force=False, sources=("yandex",))
                    self.assertIsNone(res)
                    self.assertTrue(len(yandex_called) > 0)
                    self.assertEqual(len(gismeteo_called), 0, "Фоллбеки не должны вызываться при фильтре sources=('yandex',)")

                    # Запрос конкретно ?source=gismeteo
                    res_g = caching_server.get_weather_for(56.32, 44.0, force=False, sources=("gismeteo",))
                    self.assertEqual(res_g.get("src"), "Gismeteo")
                    self.assertEqual(len(gismeteo_called), 1)

    def test_load_disk_cache_restore_ttl(self):
        """Восстановление кэша с диска: для Yandex ставит 60 мин, для фоллбека (src != Yandex) — 15 мин"""
        cfg = {"lat": 56.32, "lon": 44.0, "cache_interval_minutes": 60, "fallback_interval_minutes": 15}
        dk = caching_server.loc_key(56.32, 44.0)

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as f:
            tmp_path = f.name

        try:
            # 1. Кэш от Яндекса
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"src": "Yandex", "fact": {"temp": 21}}, f)

            with patch("caching_server.CACHE_FILE", tmp_path):
                with patch("caching_server.load_config", return_value=cfg):
                    caching_server.LOCATION_CACHE.clear()
                    caching_server.load_disk_cache()
                    entry = caching_server.LOCATION_CACHE.get(dk)
                    self.assertIsNotNone(entry)
                    self.assertEqual(entry.get("ttl"), 60 * 60, "Кэш Яндекса с диска должен получить 60 мин TTL")

            # 2. Кэш от фоллбека (Foreca)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"src": "Foreca", "fact": {"temp": 19}}, f)

            with patch("caching_server.CACHE_FILE", tmp_path):
                with patch("caching_server.load_config", return_value=cfg):
                    caching_server.LOCATION_CACHE.clear()
                    caching_server.load_disk_cache()
                    entry = caching_server.LOCATION_CACHE.get(dk)
                    self.assertIsNotNone(entry)
                    self.assertEqual(entry.get("ttl"), 15 * 60, "Кэш фоллбека с диска должен получить 15 мин probe TTL")

            # 3. Поврежденный файл не должен ломать работу
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("corrupted json {")

            with patch("caching_server.CACHE_FILE", tmp_path):
                with patch("caching_server.load_config", return_value=cfg):
                    caching_server.LOCATION_CACHE.clear()
                    caching_server.load_disk_cache()
                    self.assertNotIn(dk, caching_server.LOCATION_CACHE)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_4_primary_recovery_returns_to_yandex(self):
        """После истечения короткого probe TTL при восстановлении связи сервер возвращается на Яндекс"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"fact":{"temp":19},"forecasts":[{"parts":{}}]}'
        mock_resp.__enter__.return_value = mock_resp

        key = caching_server.loc_key(56.32, 44.0)
        # Имитируем старый фоллбек-кэш 16 минут назад (превысил 15-мин probe TTL)
        caching_server.LOCATION_CACHE[key] = {
            "data": {"src": "Gismeteo", "fact": {"temp": 15}},
            "ts": time.time() - 960,
            "ttl": 900
        }

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            with patch("caching_server.load_config", return_value={
                "api": "test-key",
                "cache_interval_minutes": 60,
                "fallback_interval_minutes": 15,
            }):
                res = caching_server.get_weather_for(56.32, 44.0, force=False)
                self.assertEqual(res.get("src"), "Yandex")
                entry = caching_server.LOCATION_CACHE.get(key)
                self.assertEqual(entry.get("ttl"), 60 * 60)

    def test_ttl_none_and_ttl_zero_handling(self):
        """ttl=None не приводит к TypeError при сравнении времени, а ttl=0 сразу экспайрит запись"""
        key = caching_server.loc_key(56.32, 44.0)
        # 1. ttl=None: должен использовать интервал по умолчанию (60 мин) без падения
        caching_server.LOCATION_CACHE[key] = {
            "data": {"src": "Yandex", "fact": {"temp": 20}},
            "ts": time.time() - 100,
            "ttl": None
        }
        with patch.object(caching_server, "_fetch_weather_locked") as mock_fetch:
            res = caching_server.get_weather_for(56.32, 44.0, force=False)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("src"), "Yandex")
            mock_fetch.assert_not_called()

        # 2. ttl=0: считается сразу истекшим
        caching_server.LOCATION_CACHE[key] = {
            "data": {"src": "Yandex", "fact": {"temp": 20}},
            "ts": time.time(),
            "ttl": 0
        }
        with patch.object(caching_server, "_fetch_weather_locked", return_value={"src": "Refreshed"}) as mock_fetch:
            res = caching_server.get_weather_for(56.32, 44.0, force=False)
            self.assertEqual(res.get("src"), "Refreshed")
            mock_fetch.assert_called_once()

    def test_clock_skew_backward_does_not_freeze_cache(self):
        """Перевод системного времени назад (ts в будущем) не замораживает кэш, а экспайрит его"""
        key = caching_server.loc_key(56.32, 44.0)
        caching_server.LOCATION_CACHE[key] = {
            "data": {"src": "OldData"},
            "ts": time.time() + 3600,  # таймстемп на час в будущем (часы перевели назад)
            "ttl": 3600
        }
        with patch.object(caching_server, "_fetch_weather_locked", return_value={"src": "FreshData"}) as mock_fetch:
            res = caching_server.get_weather_for(56.32, 44.0, force=False)
            self.assertEqual(res.get("src"), "FreshData")
            mock_fetch.assert_called_once()

    def test_key_locks_bounded_and_evicted(self):
        """KEY_LOCKS не разрастается бесконечно при запросах разных координат"""
        cfg = {"lat": 56.317722, "lon": 43.999303, "cache_interval_minutes": 60}
        with patch("caching_server.load_config", return_value=cfg):
            for i in range(40):
                lat = 50.0 + i * 0.1
                lon = 30.0 + i * 0.1
                k = caching_server.loc_key(lat, lon)
                caching_server._store_location_result(k, {"src": "OM"}, ttl=60)
            self.assertLessEqual(len(caching_server.KEY_LOCKS), 32)

    def test_cache_sharing_between_loc_and_loc_yandex(self):
        """Ответ Яндекса по общему ключу дефолтного города автоматически удовлетворяет запрос ?source=yandex без повторного API-запроса"""
        yandex_mock = MagicMock()
        yandex_mock.read.return_value = b'{"fact":{"temp":21},"forecasts":[{"parts":{}}]}'
        yandex_mock.__enter__.return_value = yandex_mock

        with patch.object(urllib.request, "urlopen", return_value=yandex_mock) as mock_url:
            with patch("caching_server.load_config", return_value={"api": "test-key", "cache_interval_minutes": 60}):
                # 1. Запрос общего ключа
                res1 = caching_server.get_weather_for(56.32, 44.0, force=False, sources=None)
                self.assertEqual(res1.get("src"), "Yandex")
                self.assertEqual(mock_url.call_count, 1)

                # 2. Запрос ?source=yandex сразу после этого: не должен делать повторный запрос в сеть
                res2 = caching_server.get_weather_for(56.32, 44.0, force=False, sources=("yandex",))
                self.assertEqual(res2.get("src"), "Yandex")
                self.assertEqual(mock_url.call_count, 1, "Запрос ?source=yandex должен взять свежий кэш Яндекса без обращения к API")

    def test_last_error_message_recorded_for_all_fallbacks(self):
        """При сбое каждого источника (Open-Meteo, OWM, 7timer, wttr) ошибка пишется в last_error_message"""
        sources_to_test = [
            ("om", "Open-Meteo", "fetch_from_openmeteo"),
            ("owm", "OpenWeatherMap", "fetch_from_openweathermap"),
            ("7timer", "7timer", "fetch_from_7timer"),
            ("wttr", "wttr.in", "fetch_from_wttr"),
        ]
        cfg = {
            "api": "test-key",
            "openweathermap_api_key": "test-owm",
            "enable_openmeteo_fallback": True,
            "enable_openweathermap_fallback": True,
            "enable_7timer_fallback": True,
            "enable_wttr_fallback": True,
        }
        with patch("caching_server.load_config", return_value=cfg):
            for src_name, label, target_fn in sources_to_test:
                caching_server.last_error_message = None
                with patch(f"caching_server.{target_fn}", side_effect=Exception(f"{src_name} exploded")):
                    caching_server.get_weather_for(56.32, 44.0, force=True, sources=(src_name,))
                    self.assertIsNotNone(caching_server.last_error_message)
                    self.assertIn(label, caching_server.last_error_message)
                    self.assertIn("exploded", caching_server.last_error_message)

    def test_http_503_response_format_and_headers(self):
        """При data is None HTTP-обработчик возвращает 503, заголовки CORS, Content-Type и JSON с error/details"""
        from io import BytesIO

        class FakeSocket:
            def __init__(self, data=b""):
                self._rfile = BytesIO(data)
                self._wfile = BytesIO()

            def settimeout(self, timeout):
                pass

            def sendall(self, data):
                self._wfile.write(data)

            def makefile(self, mode, *args, **kwargs):
                if "b" in mode:
                    return self._rfile if "r" in mode else self._wfile
                raise NotImplementedError

        req = FakeSocket(b"GET /weather.json HTTP/1.1\r\nHost: localhost\r\n\r\n")

        with patch("caching_server.get_weather_for", return_value=None):
            with patch("caching_server.last_error_message", "Mock upstream outage"):
                handler = caching_server.WeatherHTTPHandler(req, ("127.0.0.1", 12345), None)
                req._wfile.seek(0)
                resp_bytes = req._wfile.getvalue()
                resp_text = resp_bytes.decode("utf-8")

                self.assertIn("HTTP/1.0 503", resp_text)
                self.assertIn("Access-Control-Allow-Origin: *", resp_text)
                self.assertIn("Content-Type: application/json; charset=utf-8", resp_text)
                body_json = json.loads(resp_text.split("\r\n\r\n", 1)[1])
                self.assertEqual(body_json.get("error"), "Weather data unavailable")
                self.assertEqual(body_json.get("details"), "Mock upstream outage")


if __name__ == "__main__":
    unittest.main()
