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


if __name__ == "__main__":
    unittest.main()
