#!/usr/bin/env python3
"""
Unit tests for caching_server hardening:
- Body size limits (413 Payload Too Large)
- Content-Length validation (400 Bad Request)
- Concurrency limiting via CLIENT_SEMAPHORE (503 Service Unavailable)
- Atomic configuration saving and key erasing protection
"""

import os
import sys
import json
import unittest
import tempfile
import shutil

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

import caching_server


class TestServerHardening(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.orig_config_file = caching_server.CONFIG_FILE
        caching_server.CONFIG_FILE = self.config_path

        # Начальный конфиг с боевыми ключами
        initial_cfg = {
            "api": "secret_yandex_key_123",
            "gismeteo_api_key": "secret_gismeteo_key_456",
            "foreca_api_key": "secret_foreca_key_789",
            "city_name": "Нижний Новгород",
            "lat": 56.31,
            "lon": 44.00
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(initial_cfg, f)

    def tearDown(self):
        caching_server.CONFIG_FILE = self.orig_config_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_config_creates_backup(self):
        """Проверка создания .bak файла при сохранении"""
        caching_server.save_config({"city_name": "Арзамас"})
        bak_path = self.config_path + ".bak"
        self.assertTrue(os.path.isfile(bak_path), ".bak backup file must be created")
        with open(bak_path, "r", encoding="utf-8") as f:
            bak_cfg = json.load(f)
        self.assertEqual(bak_cfg["city_name"], "Нижний Новгород")

        with open(self.config_path, "r", encoding="utf-8") as f:
            new_cfg = json.load(f)
        self.assertEqual(new_cfg["city_name"], "Арзамас")

    def test_save_config_protects_keys_from_erasure(self):
        """Пустые строки и маскированные значения не должны затирать боевые ключи"""
        # Попытка стереть ключи пустой строкой, пробелами или маской
        malicious_or_broken_update = {
            "api": "",
            "gismeteo_api_key": "   ",
            "foreca_api_key": "se••••89",
            "city_name": "Бор"
        }
        caching_server.save_config(malicious_or_broken_update)

        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        self.assertEqual(cfg["api"], "secret_yandex_key_123", "Yandex API key must NOT be erased by empty string")
        self.assertEqual(cfg["gismeteo_api_key"], "secret_gismeteo_key_456", "Gismeteo key must NOT be erased by whitespace")
        self.assertEqual(cfg["foreca_api_key"], "secret_foreca_key_789", "Foreca key must NOT be overwritten by masked string")
        self.assertEqual(cfg["city_name"], "Бор", "Non-key fields must be updated")

    def test_save_config_updates_valid_new_key(self):
        """Валидный новый ключ должен корректно обновляться"""
        caching_server.save_config({"gismeteo_api_key": "new_real_key_999"})
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["gismeteo_api_key"], "new_real_key_999")

    def test_semaphore_limit_and_server_params(self):
        """Проверка лимитов параллельных соединений и параметров сервера"""
        self.assertEqual(caching_server.MAX_CONCURRENT_CLIENTS, 32)
        self.assertEqual(caching_server.WeatherHTTPHandler.timeout, 10.0)
        self.assertEqual(caching_server.WeatherHTTPHandler.MAX_BODY_SIZE, 64 * 1024)

    def test_semaphore_exhaustion_behavior(self):
        """Семафор при исчерпании блокирует новые захваты без ожидания"""
        sem = caching_server.CLIENT_SEMAPHORE
        acquired = []
        try:
            # Захватываем все 32 слота
            for _ in range(caching_server.MAX_CONCURRENT_CLIENTS):
                self.assertTrue(sem.acquire(blocking=False))
                acquired.append(True)
            # 33-й запрос должен получить отказ
            self.assertFalse(sem.acquire(blocking=False), "33rd concurrent client must be rejected")
        finally:
            for _ in acquired:
                sem.release()


    def test_save_config_explicit_clear_key(self):
        """Явная передача null или __CLEAR__ очищает ключ"""
        caching_server.save_config({"gismeteo_api_key": None, "api": "__CLEAR__"})
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["gismeteo_api_key"], "", "Explicit None must clear key to empty string")
        self.assertEqual(cfg["api"], "", "Explicit __CLEAR__ must clear key to empty string")
        self.assertEqual(cfg["foreca_api_key"], "secret_foreca_key_789", "Untouched key must remain preserved")

    def test_load_config_recovers_from_backup_on_corruption(self):
        """При повреждении config.json load_config восстанавливается из config.json.bak"""
        # Сначала создаем .bak через валидный save
        caching_server.save_config({"city_name": "Павлово"})
        bak_path = self.config_path + ".bak"
        self.assertTrue(os.path.isfile(bak_path))

        # Повреждаем основной файл мусором
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("CORRUPTED_NOT_A_JSON{{{")

        # load_config должен восстановить данные из .bak
        recovered = caching_server.load_config()
        self.assertEqual(recovered["api"], "secret_yandex_key_123")
        self.assertEqual(recovered.get("city_name"), "Нижний Новгород")


    def test_rate_limiter_is_local(self):
        """Проверка классификации локальных и внешних IP адресов"""
        rl = caching_server.RateLimiter()
        self.assertTrue(rl.is_local("127.0.0.1"))
        self.assertTrue(rl.is_local("::1"))
        self.assertTrue(rl.is_local("192.168.1.19"))
        self.assertTrue(rl.is_local("10.0.0.100"))
        self.assertTrue(rl.is_local("172.16.0.5"))
        self.assertTrue(rl.is_local("172.17.0.1"), "Docker bridge subnet must be recognized as local")
        self.assertTrue(rl.is_local("172.31.255.255"), "Upper 172.16/12 bound must be local")
        self.assertFalse(rl.is_local("172.32.0.1"), "Outside 172.16/12 must be external")
        self.assertFalse(rl.is_local("203.0.113.195"))
        self.assertFalse(rl.is_local("8.8.8.8"))
        self.assertFalse(rl.is_local(""))
        self.assertFalse(rl.is_local(None))

    def test_rate_limiter_sliding_window_throttles_external(self):
        """Внешний IP при превышении лимита получает отказ с retry_after"""
        rl = caching_server.RateLimiter()
        # Создаем dummy handler с внешним IP
        class DummyHandler:
            client_address = ("203.0.113.50", 12345)
            headers = {}

        dummy = DummyHandler()
        # Лимит для admin для внешних IP = 10 req/min
        for i in range(10):
            allowed, retry_after, limit, remaining = rl.check(dummy, "admin")
            self.assertTrue(allowed, f"Request {i+1} must be allowed")
            self.assertEqual(remaining, 10 - (i + 1))

        # 11-й запрос обязан быть отклонён
        allowed, retry_after, limit, remaining = rl.check(dummy, "admin")
        self.assertFalse(allowed, "11th request must be rejected")
        self.assertTrue(retry_after > 0, "retry_after must be positive")
        self.assertEqual(remaining, 0)

    def test_rate_limiter_zero_limit_guard(self):
        """Лимит 0 не должен вызывать IndexError на первом запросе"""
        caching_server.save_config({"rate_limit_admin_per_minute": 0})
        rl = caching_server.RateLimiter()
        class DummyHandler:
            client_address = ("203.0.113.50", 12345)
            headers = {}

        # Первый же запрос должен быть отклонён без падений
        allowed, retry_after, limit, remaining = rl.check(DummyHandler(), "admin")
        self.assertFalse(allowed)
        self.assertTrue(retry_after >= 1)
        self.assertEqual(limit, 0)
        self.assertEqual(remaining, 0)

    def test_rate_limiter_string_coercion(self):
        """Строковые числа в конфиге должны корректно приводиться к int"""
        caching_server.save_config({"rate_limit_admin_per_minute": "5"})
        rl = caching_server.RateLimiter()
        class DummyHandler:
            client_address = ("203.0.113.50", 12345)
            headers = {}

        dummy = DummyHandler()
        for _ in range(5):
            allowed, _, limit, _ = rl.check(dummy, "admin")
            self.assertTrue(allowed)
            self.assertEqual(limit, 5)
        allowed, _, _, _ = rl.check(dummy, "admin")
        self.assertFalse(allowed)

    def test_rate_limiter_disabled_in_config(self):
        """Если rate_limit_enabled: false, запросы проходят без ограничений"""
        caching_server.save_config({"rate_limit_enabled": False})
        rl = caching_server.RateLimiter()
        class DummyHandler:
            client_address = ("203.0.113.50", 12345)
            headers = {}

        for _ in range(50):
            allowed, _, _, _ = rl.check(DummyHandler(), "admin")
            self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
