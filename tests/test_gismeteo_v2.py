#!/usr/bin/env python3
"""Unit-тесты для GismeteoV2Provider."""

import sys
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

    def test_parse_forecast_aggregate_and_conversions(self):
        fake_cities = {"response": {"items": [{"id": 4355, "name": "НН"}]}}
        fake_forecast = {
            "response": {
                "items": [
                    {
                        "temperature": {"air": {"C": 18.4}},
                        "pressure": {"mm_hg_atm": 754.7},
                        "wind": {"speed": {"m_s": 3.6}, "direction": {"scale_8": 2}},
                        "humidity": {"percent": 68},
                        "icon": "c2_d",
                        "description": {"full": "Переменная облачность"}
                    },
                    {
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
            if "forecast/aggregate" in url:
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
            self.assertEqual(len(res["forecasts"][0]["hours"]), 2)

    def test_missing_api_key_raises_quota_error(self):
        empty_provider = GismeteoV2Provider(api_key="")
        with self.assertRaises(GismeteoV2QuotaError):
            empty_provider._http_get("https://api.gismeteo.net/v2/test")


if __name__ == "__main__":
    unittest.main()
