#!/usr/bin/env python3
"""Unit-тесты для GismeteoV2Provider."""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gismeteo_v2_provider import GismeteoV2Error, GismeteoV2Provider, GismeteoV2QuotaError


class GismeteoV2Tests(unittest.TestCase):
    def setUp(self):
        self.provider = GismeteoV2Provider(api_key="test-api-token")

    def test_icon_mapping_v2(self):
        mapping = {
            "c1_d": "skc_d",
            "c1_n": "skc_n",
            "c2_d": "bkn_d",
            "c2_n": "bkn_n",
            "c3_d": "ovc",
            "c3_r1_d": "bkn_ra",
            "c3_r2_d": "ovc_ra",
            "c3_s1_d": "ovc_sn",
            "c3_t1_d": "ovc_ts",
            "d.c1": "skc_d",
        }
        for code, expected in mapping.items():
            self.assertEqual(
                self.provider._map_icon(code, is_day=True),
                expected,
                f"Неверный маппинг иконки {code}"
            )

    def test_wind_direction_scale8(self):
        expected_angles = [0, 45, 90, 135, 180, 225, 270, 315]
        for scale, deg in enumerate(expected_angles):
            self.assertEqual(self.provider._wind_deg(scale), deg)
        self.assertEqual(self.provider._wind_deg(8), 0)

    def test_parse_cities_json_v2_and_cache(self):
        fake_response = {
            "response": {
                "items": [
                    {
                        "id": 4355,
                        "name": "Нижний Новгород",
                        "country": {"name": "Россия"}
                    }
                ]
            }
        }
        with patch.object(self.provider, "_http_get", return_value=fake_response) as mock_get:
            city_id = self.provider.find_city_id(56.32, 44.00)
            self.assertEqual(city_id, 4355)
            self.assertEqual(mock_get.call_count, 1)

            # Повторный вызов должен взять значение из кэша
            city_id2 = self.provider.find_city_id(56.32, 44.00)
            self.assertEqual(city_id2, 4355)
            self.assertEqual(mock_get.call_count, 1)

    def test_empty_city_search_raises_error(self):
        fake_response = {"response": {"items": []}}
        with patch.object(self.provider, "_http_get", return_value=fake_response):
            with self.assertRaises(GismeteoV2Error):
                self.provider.find_city_id(10.0, 10.0)

    def test_parse_forecast_hourly_and_conversions(self):
        # F7 (23.11): провайдер запрашивает /weather/forecast/{cid}/?days=3 -
        # почасовые пункты со скалярами; fact = пункт, ближайший к текущему времени.
        fake_cities = {"response": {"items": [{"id": 4355, "name": "НН"}]}}
        now = int(time.time())
        fake_forecast = {
            "response": {
                "items": [
                    {
                        "date": {"unix": now - 600, "local": "2026-11-23 12:00:00"},
                        "temperature": {"air": {"C": 18.4}},
                        "pressure": {"mm_hg_atm": 754.7},
                        "wind": {"speed": {"m_s": 3.6}, "direction": {"scale_8": 2}},
                        "humidity": {"percent": 68},
                        "icon": "c2_d",
                        "description": {"full": "Переменная облачность"}
                    },
                    {
                        "date": {"unix": now + 3 * 3600, "local": "2026-11-23 15:00:00"},
                        "temperature": {"air": {"C": 15.1}},
                        "pressure": {"mm_hg_atm": 755.2},
                        "wind": {"speed": {"m_s": 2.1}, "direction": {"scale_8": 4}},
                        "humidity": {"percent": 75},
                        "icon": "c3_r1_d",
                        "description": {"full": "Небольшой дождь"}
                    }
                ]
            }
        }

        def mock_http(url, *args, **kwargs):
            if "search/cities" in url:
                return fake_cities
            if "/weather/forecast/" in url:
                return fake_forecast
            raise ValueError(f"Unexpected url: {url}")

        with patch.object(self.provider, "_http_get", side_effect=mock_http):
            res = self.provider.get_weather(56.32, 44.00)
            self.assertEqual(res["src"], "Gismeteo")
            self.assertEqual(res.get("city_name"), "НН")
            fact = res["fact"]
            self.assertEqual(fact["temp"], 18)
            self.assertEqual(fact["pressure_mm"], 755)
            self.assertEqual(fact["wind_speed"], 3.6)
            self.assertEqual(fact["wind_angle"], 90)  # scale_8: 2 -> 90 градусов
            self.assertEqual(fact["humidity"], 68)
            self.assertEqual(fact["icon"], "bkn_d")
            hours = res["forecasts"][0]["hours"]
            self.assertEqual(len(hours), 2)
            self.assertEqual(hours[0]["hour"], 12)  # час из date.local
            self.assertEqual(hours[1]["hour"], 15)
            self.assertEqual(hours[1]["temp"], 15)

    def test_aggregate_shape_does_not_crash(self):
        # F7 (23.11): реальная форма /weather/forecast/aggregate/ (суточные
        # {min,max,avg}-агрегаты из fixture 23.11) раньше роняла парсер
        # (TypeError: float() argument ... dict). Теперь извлечение должно
        # пройти без исключения на дефолтах.
        fake_cities = {"response": {"items": [{"id": 4355, "name": "НН"}]}}
        now = int(time.time())
        fake_forecast = {
            "response": {
                "items": [
                    {
                        "date": {"unix": now, "local": "2026-11-23 00:00:00"},
                        "temperature": {
                            "air": {
                                "min": {"C": 0.0, "F": 32.1},
                                "max": {"C": 1.9, "F": 35.5},
                                "avg": {"C": 1.0, "F": 33.9}
                            }
                        },
                        "pressure": {"mm_hg_atm": {"min": 747, "max": 749}},
                        "humidity": {"percent": {"min": 74, "max": 86, "avg": 78}},
                        "wind": {
                            "speed": {"max": {"m_s": 1}, "min": {"m_s": 0}},
                            "direction": {"avg": {"scale_8": 6}}
                        },
                        "icon": "c3_rs1"
                    }
                ]
            }
        }

        def mock_http(url, *args, **kwargs):
            if "search/cities" in url:
                return fake_cities
            if "/weather/forecast/" in url:
                return fake_forecast
            raise ValueError(f"Unexpected url: {url}")

        with patch.object(self.provider, "_http_get", side_effect=mock_http):
            res = self.provider.get_weather(56.32, 44.00)
            fact = res["fact"]
            self.assertEqual(fact["temp"], 1)      # avg из агрегата
            self.assertEqual(fact["pressure_mm"], 747)  # min-фолбэк
            self.assertEqual(fact["humidity"], 78)  # avg
            self.assertEqual(fact["wind_speed"], 0.0)  # min m_s
            self.assertEqual(fact["wind_angle"], 270)  # scale_8 avg=6 -> 270
            self.assertEqual(fact["condition"], "Ясно")  # description отсутствует
            hours = res["forecasts"][0]["hours"]
            self.assertEqual(len(hours), 1)
            for h in hours:
                self.assertIsInstance(h["temp"], int)
                self.assertIsInstance(h["pressure_mm"], int)
                self.assertIsInstance(h["humidity"], int)
                self.assertIsInstance(h["wind_speed"], float)

    def test_num_helper_rejects_garbage(self):
        # Хелперы F7: незнакомые формы дают None (а не исключение)
        self.assertIsNone(self.provider._num("abc"))
        self.assertIsNone(self.provider._num({"unknown": "x"}))
        self.assertIsNone(self.provider._num(True))
        self.assertIsNone(self.provider._num(None))
        self.assertIsNone(self.provider._num_at("not-a-dict", "temperature"))
        self.assertEqual(self.provider._num(18), 18.0)
        self.assertEqual(self.provider._num({"avg": 1, "min": 0}), 1.0)

    def test_missing_api_key_raises_quota_error(self):
        empty_provider = GismeteoV2Provider(api_key="")
        with self.assertRaises(GismeteoV2QuotaError):
            empty_provider._http_get("https://api.gismeteo.net/v2/test")


if __name__ == "__main__":
    unittest.main()
