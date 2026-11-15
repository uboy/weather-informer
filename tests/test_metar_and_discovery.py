#!/usr/bin/env python3
"""
Unit and integration tests for UDP autodiscovery, NOAA METAR collection,
multi-tier geolocation, and configuration endpoints.
"""

import os
import sys
import json
import time
import socket
import sqlite3
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import caching_server


class TestMetarAndDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls.test_dir, "config.json")
        cls.orig_config_file = caching_server.CONFIG_FILE
        caching_server.CONFIG_FILE = cls.config_path

        cls.db_path = os.path.join(cls.test_dir, "forecast.db")
        cls.orig_db_file = caching_server.FORECAST_DB
        caching_server.FORECAST_DB = cls.db_path

        # Initial config
        initial_cfg = {
            "port": 0,
            "api": "test_api_key_12345",
            "lat": 56.317722,
            "lon": 43.999303,
            "city_name": "Нижний Новгород",
            "station_altitude": 160.0
        }
        with open(cls.config_path, "w", encoding="utf-8") as f:
            json.dump(initial_cfg, f)

    @classmethod
    def tearDownClass(cls):
        caching_server.CONFIG_FILE = cls.orig_config_file
        caching_server.FORECAST_DB = cls.orig_db_file
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_haversine_and_nearest_icao(self):
        # 1. Точные координаты Нижнего Новгорода
        ap_nn = caching_server.find_nearest_icao(56.317722, 43.999303)
        self.assertIsNotNone(ap_nn)
        self.assertEqual(ap_nn["icao"], "UWGG")
        self.assertLess(ap_nn["distance_km"], 30.0)

        # 2. Координаты Москвы (Красная площадь)
        ap_msk = caching_server.find_nearest_icao(55.7539, 37.6208)
        self.assertIsNotNone(ap_msk)
        self.assertIn(ap_msk["icao"], ["UUEE", "UUDD", "UUWW", "UUBW"])
        self.assertLess(ap_msk["distance_km"], 40.0)

        # 3. Координаты Санкт-Петербурга
        ap_spb = caching_server.find_nearest_icao(59.9343, 30.3351)
        self.assertIsNotNone(ap_spb)
        self.assertEqual(ap_spb["icao"], "ULLI")
        self.assertLess(ap_spb["distance_km"], 30.0)

    def test_fetch_noaa_metar_parsing(self):
        mock_awc_json = json.dumps([
            {
                "icaoId": "UWGG",
                "temp": 3.0,
                "dewp": -1.0,
                "wdir": 270,
                "wspd": 6.0,
                "altim": 1020.0,
                "elev": 78,
                "name": "Nizhny Novgorod Intl, RU",
                "reportTime": "2026-11-15T15:30:00Z",
                "rawOb": "METAR UWGG 151530Z 27006MPS 9999 BKN030 03/M01 Q1020 NOSIG"
            }
        ]).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_awc_json
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            metar = caching_server.fetch_noaa_metar("UWGG")
            self.assertIsNotNone(metar)
            self.assertEqual(metar["icao"], "UWGG")
            self.assertEqual(metar["temp"], 3.0)
            self.assertEqual(metar["dewp"], -1.0)
            self.assertIsNotNone(metar["humidity"])
            self.assertGreater(metar["humidity"], 70)
            self.assertLess(metar["humidity"], 85)
            self.assertEqual(metar["pressure_msl_hpa"], 1020.0)
            # Приведение 1020 гПа (уровень моря) к высоте аэродрома 78м дает ~758 мм
            self.assertIsNotNone(metar["pressure_mm"])
            self.assertIn(metar["pressure_mm"], [757, 758, 759])

    def test_collect_noaa_metar_db(self):
        mock_awc_json = json.dumps([
            {
                "icaoId": "UWGG",
                "temp": 2.5,
                "dewp": -2.0,
                "altim": 1021.0,
                "elev": 78,
                "name": "Nizhny Novgorod",
                "reportTime": "2026-11-15T17:00:00Z",
                "rawOb": "SPECI UWGG 151700Z Q1021"
            }
        ]).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_awc_json
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = caching_server.collect_noaa_metar()
            self.assertIsNotNone(res)

            conn = sqlite3.connect(caching_server.FORECAST_DB)
            row = conn.execute("SELECT station, source, temperature, pressure, phys_station FROM observations WHERE source='metar-awc'").fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertIn("UWGG", row[0])
            self.assertEqual(row[1], "metar-awc")
            self.assertEqual(row[2], 2.5)
            self.assertEqual(row[4], "UWGG")

    def test_detect_location_by_ip(self):
        mock_ip_api = json.dumps({
            "status": "success",
            "lat": 56.3269,
            "lon": 44.0059,
            "city": "Nizhny Novgorod",
            "timezone": "Europe/Moscow"
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_ip_api
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            loc = caching_server.detect_location_by_ip()
            self.assertIsNotNone(loc)
            self.assertEqual(loc["source"], "ip-api")
            self.assertEqual(loc["city"], "Nizhny Novgorod")
            self.assertAlmostEqual(loc["lat"], 56.3269, places=3)
            self.assertAlmostEqual(loc["lon"], 44.0059, places=3)

    def test_udp_discovery_responder(self):
        # Подбираем свободный порт для UDP
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        test_sock.bind(("127.0.0.1", 0))
        test_port = test_sock.getsockname()[1]
        test_sock.close()

        caching_server.start_udp_discovery_responder(port=test_port)
        time.sleep(0.15)

        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(2.0)
            # Отправка текстового discovery запроса
            client.sendto(b"WEATHER_DISCOVERY", ("127.0.0.1", test_port))
            data, _ = client.recvfrom(2048)
            resp = json.loads(data.decode("utf-8"))
            self.assertEqual(resp.get("status"), "ok")
            self.assertEqual(resp.get("server"), "weather-cache")
            self.assertEqual(resp.get("city"), "Нижний Новгород")

            # Отправка JSON discovery запроса
            client.sendto(b'{"cmd":"discover"}', ("127.0.0.1", test_port))
            data, _ = client.recvfrom(2048)
            resp2 = json.loads(data.decode("utf-8"))
            self.assertEqual(resp2.get("status"), "ok")
            client.close()
        finally:
            caching_server.stop_udp_discovery_responder()


if __name__ == "__main__":
    unittest.main()
