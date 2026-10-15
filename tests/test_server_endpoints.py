#!/usr/bin/env python3
"""
Comprehensive HTTP integration test suite for caching_server.py.
Spins up a real BoundedThreadingHTTPServer on an ephemeral port (127.0.0.1:0)
and tests all endpoints exposed by the service:
1. GET /health and /status
2. GET /settings (HTML)
3. GET /api/settings (masked JSON)
4. POST /api/settings (valid update, key protection)
5. POST /api/settings (payload > 64KB -> 413, negative -> 400, invalid JSON)
6. GET /weather.json (default coords, custom coords, invalid coords -> 400, unknown source -> 400)
7. GET /geocode (validation for short query)
8. GET /reverse (validation for coords)
9. GET /accuracy (validation for missing parameters)
10. GET /install (HTML with instructions)
11. GET /informer.apk (binary APK download)
12. GET /unknown (404 Not Found)
"""

import os
import sys
import json
import time
import http.client
import threading
import tempfile
import shutil
import unittest

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import caching_server


class TestServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls.test_dir, "config.json")
        cls.orig_config_file = caching_server.CONFIG_FILE
        caching_server.CONFIG_FILE = cls.config_path

        # Initial test config
        initial_cfg = {
            "port": 0,
            "api": "test_yandex_secret",
            "gismeteo_api_key": "test_gismeteo_secret",
            "foreca_api_key": "test_foreca_secret",
            "openweathermap_api_key": "test_owm_secret",
            "city_name": "Нижний Новгород",
            "lat": 56.317722,
            "lon": 43.999303,
            "primary_source": "yandex",
            "cache_interval_minutes": 60,
            "fallback_interval_minutes": 15
        }
        with open(cls.config_path, "w", encoding="utf-8") as f:
            json.dump(initial_cfg, f)

        # Populate cache with mock data
        cls.mock_data = {
            "now": 1700000000,
            "city_name": "Нижний Новгород",
            "src": "Yandex",
            "fact": {"temp": 12, "pressure_mm": 745, "humidity": 60, "wind_speed": 3}
        }
        caching_server.cached_data = cls.mock_data
        caching_server.last_fetch_time = time.time()
        caching_server.last_error_message = None

        # Start server on ephemeral port (port 0 selects free kernel port)
        cls.server = caching_server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), caching_server.WeatherHTTPHandler
        )
        cls.port = cls.server.server_address[1]

        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        caching_server.CONFIG_FILE = cls.orig_config_file
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        caching_server.cached_data = self.mock_data
        caching_server.last_fetch_time = time.time()
        caching_server.last_error_message = None
        # Seed cache for coordinates tested to prevent live outbound API calls
        with caching_server.LOCATION_CACHE_LOCK:
            for lat, lon in [(56.317722, 43.999303), (56.24, 43.46), (56.31, 44.00), (56.32, 44.0)]:
                k = caching_server.loc_key(lat, lon)
                caching_server.LOCATION_CACHE[k] = {"data": caching_server.cached_data, "ts": time.time(), "ttl": 3600}

    def _request(self, method, path, data_dict=None, raw_body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        body = raw_body
        if data_dict is not None:
            body = json.dumps(data_dict).encode("utf-8")
            if "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json; charset=utf-8"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, resp_headers, data

    def _get(self, path, headers=None):
        return self._request("GET", path, headers=headers)

    def _post(self, path, data_dict=None, raw_body=None, headers=None):
        return self._request("POST", path, data_dict=data_dict, raw_body=raw_body, headers=headers)

    # 1. Health & Status
    def test_health_and_status(self):
        for path in ("/health", "/status"):
            status, _, body = self._get(path)
            self.assertEqual(status, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")
            self.assertTrue(data.get("has_weather_data"))
            self.assertIn("cache_age_minutes", data)

    # 2. Settings HTML
    def test_settings_html(self):
        status, headers, body = self._get("/settings")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        html = body.decode("utf-8")
        self.assertIn("Настройки", html)

    # 3. GET /api/settings (маскированные ключи)
    def test_get_api_settings_masks_secrets(self):
        status, _, body = self._get("/api/settings")
        self.assertEqual(status, 200)
        cfg = json.loads(body.decode("utf-8"))
        self.assertIn("city_name", cfg)
        # Ключи обязаны быть маскированы (не в открытом виде)
        self.assertNotIn("test_yandex_secret", cfg["api"])
        self.assertIn("•", cfg["api"])
        self.assertNotIn("test_gismeteo_secret", cfg["gismeteo_api_key"])

    # 4. POST /api/settings (успешное обновление)
    def test_post_api_settings_valid(self):
        update = {"city_name": "Дзержинск", "lat": 56.24, "lon": 43.46}
        status, _, body = self._post("/api/settings", data_dict=update)
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertEqual(res.get("status"), "saved")

        # Проверяем что на сервере обновилось
        status, _, body = self._get("/api/settings")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["city_name"], "Дзержинск")

    # 5. POST /api/settings безопасность: 413 при >64KB, 400 при отрицательном размере
    def test_post_api_settings_security_limits(self):
        # 5a. Payload Too Large (>64KB)
        big_body = b"X" * (65 * 1024)
        status, _, _ = self._post("/api/settings", raw_body=big_body)
        self.assertEqual(status, 413)

        # 5b. Negative Content-Length -> 400
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/settings")
        conn.putheader("Content-Length", "-10")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"Negative Content-Length", resp.read())
        conn.close()

        # 5c. Malformed JSON -> 400 Bad Request
        status, _, body = self._post("/api/settings", raw_body=b"{malformed json...")
        self.assertEqual(status, 400)
        self.assertIn(b"Invalid JSON payload", body)

        # 5d. Explicit key wipe via __CLEAR__ -> 200 and key cleared
        status, _, _ = self._post("/api/settings", data_dict={"gismeteo_api_key": "__CLEAR__"})
        self.assertEqual(status, 200)
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["gismeteo_api_key"], "")

    # 5e. Server saturation HTTP 503 fast-fail and exact Content-Length
    def test_server_overload_503(self):
        sem = caching_server.CLIENT_SEMAPHORE
        slots = 0
        try:
            time.sleep(0.05)
            while sem.acquire(blocking=False):
                slots += 1
            self.assertTrue(slots > 0, "Should have acquired at least one slot")

            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 503)
            self.assertEqual(resp.getheader("retry-after"), "5")
            data = resp.read()
            self.assertEqual(len(data), int(resp.getheader("content-length")))
            self.assertIn(b"Server overloaded", data)
            conn.close()
        finally:
            for _ in range(slots):
                sem.release()

    # 6. GET /weather.json (кэш, валидация)
    def test_weather_endpoints(self):
        # 6a. Нормальный запрос
        status, _, body = self._get("/weather.json")
        self.assertEqual(status, 200)
        w = json.loads(body.decode("utf-8"))
        self.assertIn("now", w)
        self.assertIn("fact", w)

        # 6b. Синонимы маршрута
        for p in ("/forecast.json", "/v2/forecast"):
            st, _, _ = self._get(p)
            self.assertEqual(st, 200)

        # 6c. Ошибочные координаты -> 400
        st, _, _ = self._get("/weather.json?lat=invalid&lon=44.0")
        self.assertEqual(st, 400)

        # 6d. Неизвестный источник -> 400
        st, _, _ = self._get("/weather.json?source=mars_weather")
        self.assertEqual(st, 400)

    # 7. GET /geocode валидация длины запроса
    def test_geocode_validation(self):
        status, _, _ = self._get("/geocode?q=a")
        self.assertEqual(status, 400)

    # 8. GET /reverse валидация координат
    def test_reverse_validation(self):
        status, _, _ = self._get("/reverse?lat=abc&lon=def")
        self.assertEqual(status, 400)

    # 9. GET /accuracy валидация параметров
    def test_accuracy_validation(self):
        status, _, _ = self._get("/accuracy")
        self.assertEqual(status, 400)

    # 10. GET /install
    def test_install_page(self):
        status, _, body = self._get("/install")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("Установка Погодного Информера", html)
        self.assertIn("informer.apk", html)

    # 11. GET /informer.apk
    def test_informer_apk_download(self):
        status, headers, body = self._get("/informer.apk")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "application/vnd.android.package-archive")
        self.assertTrue(len(body) > 50000, "APK must be a valid binary > 50KB")

    # 12. GET /unknown_route -> 404
    def test_unknown_route_returns_404(self):
        status, _, _ = self._get("/some_random_scanner_path")
        self.assertEqual(status, 404)


    # 13. Rate Limiter HTTP 429 Too Many Requests
    def test_rate_limiter_http_429(self):
        # Внешний IP через X-Forwarded-For на категорию admin (лимит 10 req/min)
        ext_headers = {"X-Forwarded-For": "198.51.100.222"}
        for i in range(10):
            st, hdrs, _ = self._get("/accuracy?provider=foreca&lead_hours=24", headers=ext_headers)
            self.assertNotEqual(st, 429, f"Request {i+1} must not be rate limited")

        # 11-й запрос обязан вернуть 429 Too Many Requests
        st, hdrs, body = self._get("/accuracy?provider=foreca&lead_hours=24", headers=ext_headers)
        self.assertEqual(st, 429)
        self.assertTrue(int(hdrs.get("retry-after", 0)) > 0)
        self.assertEqual(hdrs.get("x-ratelimit-remaining"), "0")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("error"), "Too Many Requests")
        self.assertEqual(data.get("category"), "admin")


if __name__ == "__main__":
    unittest.main()
