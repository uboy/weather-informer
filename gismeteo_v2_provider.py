#!/usr/bin/env python3
"""Провайдер погоды Gismeteo официального REST API v2 (api.gismeteo.net).

Использует заголовок авторизации X-Gismeteo-Token.
Обеспечивает маппинг в формат информера, локальное кэширование city_id
и строгую классификацию ошибок (GismeteoV2QuotaError для 401/403/429).
"""

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("gismeteo_v2")


class GismeteoV2Error(Exception):
    """Базовая ошибка провайдера Gismeteo v2."""
    pass


class GismeteoV2QuotaError(GismeteoV2Error):
    """Ошибка исчерпания суточной квоты или недействительного ключа (401/403/429)."""
    pass


def _moon_code(dt=None):
    if dt is None:
        dt = datetime.now()
    ref = datetime(2000, 1, 6, 18, 14)
    days = (dt - ref).total_seconds() / 86400.0
    frac = (days % 29.530588853) / 29.530588853
    return int(round(frac * 8)) % 8


class GismeteoV2Provider:
    BASE_URL = "https://api.gismeteo.net/v2"
    USER_AGENT = "WeatherInformerLocal/2.0 (lan weather kiosk; gismeteo official v2)"
    WD_DEG = [0, 45, 90, 135, 180, 225, 270, 315]

    # Таблица соответствия кодов иконок Gismeteo v2 -> формат Яндекс/информера
    ICON_MAP = {
        "c1_d": "skc_d",
        "c1_n": "skc_n",
        "c2_d": "bkn_d",
        "c2_n": "bkn_n",
        "c3_d": "ovc",
        "c3_n": "ovc",
        "c3": "ovc",
        "c3_r1_d": "bkn_ra",
        "c3_r1_n": "bkn_ra",
        "c3_r2_d": "ovc_ra",
        "c3_r2_n": "ovc_ra",
        "c3_r3_d": "ovc_ra",
        "c3_r3_n": "ovc_ra",
        "c3_s1_d": "ovc_sn",
        "c3_s1_n": "ovc_sn",
        "c3_s2_d": "ovc_sn",
        "c3_s2_n": "ovc_sn",
        "c3_s3_d": "ovc_sn",
        "c3_s3_n": "ovc_sn",
        "c3_st_d": "ovc_sn",
        "c3_st_n": "ovc_sn",
        "c3_t1_d": "ovc_ts",
        "c3_t1_n": "ovc_ts",
        "c3_t2_d": "ovc_ts",
        "c3_t2_n": "ovc_ts",
        "c3_t3_d": "ovc_ts",
        "c3_t3_n": "ovc_ts",
        "d.c1": "skc_d",
        "n.c1": "skc_n",
        "d.c2": "bkn_d",
        "n.c2": "bkn_n",
        "d.c3": "ovc",
        "n.c3": "ovc",
    }

    def __init__(self, api_key="", cache_path=None, logger=None):
        self.api_key = (api_key or "").strip()
        self.cache_path = cache_path
        self.logger = logger or log
        self._city_cache = {}
        self._load_city_cache()

    def _load_city_cache(self):
        if self.cache_path and os.path.isfile(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._city_cache = data
            except Exception as e:
                self.logger.warning("GismeteoV2: не удалось загрузить city_cache: %s", e)

    def _save_city_cache(self):
        if self.cache_path:
            try:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self._city_cache, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.logger.warning("GismeteoV2: не удалось сохранить city_cache: %s", e)

    def _http_get(self, url, timeout=10):
        if not self.api_key:
            raise GismeteoV2QuotaError("GismeteoV2: api_key не задан")

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.USER_AGENT,
                "X-Gismeteo-Token": self.api_key,
                "Accept": "application/json",
                "Accept-Encoding": "deflate, gzip",
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_data = resp.read()
                # Распаковка если gzip
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    raw_data = gzip.decompress(raw_data)
                elif resp.info().get("Content-Encoding") == "deflate":
                    import zlib
                    try:
                        raw_data = zlib.decompress(raw_data)
                    except zlib.error:
                        raw_data = zlib.decompress(raw_data, -zlib.MAX_WBITS)
                return json.loads(raw_data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise GismeteoV2QuotaError(f"GismeteoV2 HTTP {e.code} (квота/доступ): {e.reason}")
            raise GismeteoV2Error(f"GismeteoV2 HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise GismeteoV2Error(f"GismeteoV2 сетевая ошибка: {e.reason}")
        except json.JSONDecodeError as e:
            raise GismeteoV2Error(f"GismeteoV2 невалидный JSON: {e}")
        except Exception as e:
            raise GismeteoV2Error(f"GismeteoV2 сбой запроса: {e}")

    @classmethod
    def _map_icon(cls, code, is_day=True):
        if not code:
            return "skc_d" if is_day else "skc_n"
        code_clean = str(code).strip().lower()
        if code_clean in cls.ICON_MAP:
            return cls.ICON_MAP[code_clean]
        
        # Разбор составных кодов
        suffix = "_d" if is_day else "_n"
        if "t" in code_clean:
            return "ovc_ts"
        if "s" in code_clean:
            return "ovc_sn"
        if "r" in code_clean:
            return "ovc_ra"
        if "c3" in code_clean:
            return "ovc"
        if "c2" in code_clean:
            return f"bkn{suffix}"
        if "c1" in code_clean:
            return f"skc{suffix}"
        return f"skc{suffix}"

    @classmethod
    def _wind_deg(cls, scale_8_val):
        try:
            idx = int(scale_8_val) % 8
            return cls.WD_DEG[idx]
        except (TypeError, ValueError):
            return 0

    def find_city_id(self, lat, lon):
        cache_key = f"{round(float(lat), 2)}:{round(float(lon), 2)}"
        if cache_key in self._city_cache:
            return self._city_cache[cache_key]["id"]

        url = f"{self.BASE_URL}/search/cities/?latitude={lat}&longitude={lon}&limit=1"
        data = self._http_get(url)
        
        # Ответ обычно {"response": {"items": [...]}} или {"items": [...]} или список
        items = []
        if isinstance(data, dict):
            resp = data.get("response", data)
            if isinstance(resp, dict):
                items = resp.get("items", [])
            elif isinstance(resp, list):
                items = resp
        elif isinstance(data, list):
            items = data

        if not items or not isinstance(items[0], dict) or "id" not in items[0]:
            raise GismeteoV2Error(f"GismeteoV2: не найден город по координатам {lat}, {lon}")

        city_info = items[0]
        city_id = city_info["id"]
        self._city_cache[cache_key] = {
            "id": city_id,
            "name": city_info.get("name", ""),
            "country": city_info.get("country", {}).get("name", ""),
            "cached_at": time.time(),
        }
        self._save_city_cache()
        return city_id

    def get_weather(self, lat, lon, city_name=None):
        city_id = self.find_city_id(lat, lon)
        
        # Запрашиваем агрегированный прогноз на 3 дня и текущие
        forecast_url = f"{self.BASE_URL}/weather/forecast/aggregate/{city_id}/?days=3"
        fc_data = self._http_get(forecast_url)
        
        resp = fc_data.get("response", fc_data)
        items = resp.get("items", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
        
        if not items:
            raise GismeteoV2Error(f"GismeteoV2: пустой ответ прогноза для города {city_id}")

        cur = items[0]
        # Извлекаем fact
        temp_val = cur.get("temperature", {}).get("air", {}).get("C")
        if temp_val is None:
            temp_val = cur.get("temperature", {}).get("comfort", {}).get("C", 0)
        temp_int = int(round(float(temp_val))) if temp_val is not None else 0

        # Давление
        pressure_val = cur.get("pressure", {}).get("mm_hg_atm")
        if pressure_val is None:
            pressure_val = cur.get("pressure", {}).get("h_pa", 1013) * 0.750062
        pressure_int = int(round(float(pressure_val)))

        # Ветер
        wind_speed = float(cur.get("wind", {}).get("speed", {}).get("m_s", 0.0))
        wind_scale8 = cur.get("wind", {}).get("direction", {}).get("scale_8", 0)
        wind_deg = self._wind_deg(wind_scale8)

        # Влажность
        humidity = int(cur.get("humidity", {}).get("percent", 0))

        # Иконка и описание
        icon_code = cur.get("icon", "")
        descr = cur.get("description", {}).get("full", "Ясно")
        icon_mapped = self._map_icon(icon_code, is_day=True)

        fact = {
            "temp": temp_int,
            "icon": icon_mapped,
            "wind_speed": wind_speed,
            "wind_angle": wind_deg,
            "humidity": humidity,
            "pressure_mm": pressure_int,
            "condition": descr,
        }

        # Формируем почасовку и parts
        hours = []
        parts = {
            "night": {"temp_avg": temp_int, "icon": self._map_icon(icon_code, False), "condition": descr, "wind_speed": wind_speed, "pressure_mm": pressure_int, "humidity": humidity},
            "morning": {"temp_avg": temp_int, "icon": self._map_icon(icon_code, True), "condition": descr, "wind_speed": wind_speed, "pressure_mm": pressure_int, "humidity": humidity},
            "day": {"temp_avg": temp_int, "icon": self._map_icon(icon_code, True), "condition": descr, "wind_speed": wind_speed, "pressure_mm": pressure_int, "humidity": humidity},
            "evening": {"temp_avg": temp_int, "icon": self._map_icon(icon_code, False), "condition": descr, "wind_speed": wind_speed, "pressure_mm": pressure_int, "humidity": humidity},
        }

        for idx, item in enumerate(items[:24]):
            t_air = item.get("temperature", {}).get("air", {}).get("C", temp_int)
            p_val = item.get("pressure", {}).get("mm_hg_atm", pressure_int)
            w_spd = item.get("wind", {}).get("speed", {}).get("m_s", wind_speed)
            w_dir = self._wind_deg(item.get("wind", {}).get("direction", {}).get("scale_8", 0))
            hum = item.get("humidity", {}).get("percent", humidity)
            ic = self._map_icon(item.get("icon", icon_code), is_day=(6 <= idx % 24 < 22))
            cond = item.get("description", {}).get("full", descr)
            hours.append({
                "hour": idx % 24,
                "temp": int(round(float(t_air))),
                "icon": ic,
                "condition": cond,
                "wind_speed": float(w_spd),
                "wind_angle": w_dir,
                "pressure_mm": int(round(float(p_val))),
                "humidity": int(hum),
            })

        forecasts = [
            {
                "sunrise": "06:00",
                "sunset": "19:00",
                "moon_code": _moon_code(),
                "hours": hours,
                "parts": parts,
            },
            {
                "parts": parts
            }
        ]

        return {
            "src": "Gismeteo",
            "now": int(time.time()),
            "fact": fact,
            "forecasts": forecasts,
            "gismeteo_v2": True,
        }
