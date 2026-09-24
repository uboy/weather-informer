#!/usr/bin/env python3
"""
WeatherInformer Local Caching Proxy Server.

Кэширующий прокси-сервер погоды для локальной сети.
- Забирает погоду из Яндекс.Погоды раз в 90 минут (16 запросов в сутки, квота 30/день).
- Раздаёт кэш неограниченному числу планшетов и клиентов в локальной сети.
- Если Яндекс возвращает 403/ошибку, автоматически переключается на Open-Meteo (безлимитно).
- Сохраняет кэш на диск, чтобы при перезапуске сервера не тратить запросы.
"""

import os
import sys
import json
import time
import threading
import logging
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("WeatherCache")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, "weather_cache.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "weather_stats.csv")
STATS_LOCK = threading.Lock()

# Дефолтные параметры
DEFAULT_CONFIG = {
    "port": 8085,
    "cache_interval_minutes": 90,  # 16 запросов в сутки (квота 30/день)
    "api": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "lat": 56.317722,
    "lon": 43.999303,
    "enable_openmeteo_fallback": True,
    "om_proxy": "",
    "enable_wttr_fallback": True,
    "enable_7timer_fallback": True,
    "stats_enabled": True,
    "stats_interval_minutes": 60
}

cached_data = None
last_fetch_time = 0
last_error_message = None
fetch_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
                log.info("Loaded config from %s", CONFIG_FILE)
        except Exception as e:
            log.warning("Could not parse %s: %s (using defaults)", CONFIG_FILE, e)
    return cfg


def normalize_weather_data(data):
    if not isinstance(data, dict):
        return data
    fact = data.get("fact")
    info = data.get("info", {})
    if isinstance(fact, dict):
        if "pressure_mm" not in fact:
            if "def_pressure_mm" in info:
                fact["pressure_mm"] = info["def_pressure_mm"]
            elif "def_pressure_pa" in info:
                fact["pressure_mm"] = round(info["def_pressure_pa"] * 0.750062)
            elif "pressure_pa" in fact:
                fact["pressure_mm"] = round(fact["pressure_pa"] * 0.750062)
            else:
                fact["pressure_mm"] = 748
    return data


def load_disk_cache():
    global cached_data, last_fetch_time
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "fact" in data:
                    cached_data = normalize_weather_data(data)
                    last_fetch_time = os.path.getmtime(CACHE_FILE)
                    log.info("Restored cache from disk (%s), age: %.1f min",
                             CACHE_FILE, (time.time() - last_fetch_time) / 60)
        except Exception as e:
            log.warning("Could not read disk cache: %s", e)


def save_disk_cache(data):
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log.warning("Could not save cache to disk: %s", e)


def stats_log_source(source, data):
    """Лог факта и прогнозов (+3/+6 ч) источника в CSV (нормализованный формат Яндекса)"""
    try:
        now = datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        temp_now = (data.get("fact") or {}).get("temp")
        forecasts = data.get("forecasts") or []
        hours = (forecasts[0].get("hours") if forecasts else []) or []
        rows = []
        if temp_now is not None:
            rows.append((now_iso, source, "actual", now_iso, temp_now))
        for horizon in (3, 6):
            target = now + timedelta(hours=horizon)
            tval = None
            for h in hours:
                try:
                    if int(h.get("hour", -1)) == target.hour:
                        tval = h.get("temp")
                        break
                except (TypeError, ValueError):
                    pass
            if tval is not None:
                rows.append((now_iso, source, "forecast",
                             target.replace(microsecond=0).isoformat(timespec="seconds"), tval))
        if not rows:
            return
        with STATS_LOCK:
            new_file = not os.path.isfile(STATS_FILE)
            with open(STATS_FILE, "a", encoding="utf-8") as f:
                if new_file:
                    f.write("ts,source,kind,target,temp_c\n")
                for r in rows:
                    f.write(",".join(str(x) for x in r) + "\n")
        log.info("stats: logged %d rows for %s", len(rows), source)
    except Exception as e:
        log.warning("stats_log_source(%s) failed: %s", source, e)


def stats_collect_non_yandex(cfg):
    """Сбор фактов+прогнозов бесплатных источников (квоту Яндекса не тратим)"""
    lat = cfg.get("lat", 56.317722)
    lon = cfg.get("lon", 43.999303)
    proxy = cfg.get("om_proxy", "") or None
    for name, fn, args in (
        ("Open-Meteo", fetch_from_openmeteo, (lat, lon, proxy)),
        ("wttr.in", fetch_from_wttr, (lat, lon)),
        ("7timer", fetch_from_7timer, (lat, lon)),
    ):
        try:
            stats_log_source(name, fn(*args))
        except Exception as e:
            log.info("stats: %s unavailable: %s", name, e)


def stats_loop():
    time.sleep(120)
    while True:
        interval = 3600
        try:
            cfg = load_config()
            if cfg.get("stats_enabled", True):
                stats_collect_non_yandex(cfg)
                interval = max(300, int(cfg.get("stats_interval_minutes", 60)) * 60)
        except Exception as e:
            log.error("stats loop error: %s", e)
        time.sleep(interval)


def fetch_from_openmeteo(lat, lon, proxy=None):
    """Fallback-источник Open-Meteo с конвертацией в формат Яндекс.Погоды"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&daily=sunrise,sunset,temperature_2m_max,temperature_2m_min"
        "&hourly=temperature_2m,relativehumidity_2m,surface_pressure,windspeed_10m,winddirection_10m,weathercode"
        "&current_weather=true&timezone=auto"
    )
    om = json.loads(fetch_url_via(url, proxy, 20))

    cw = om.get("current_weather", {})
    daily = om.get("daily", {})
    hourly = om.get("hourly", {})

    temp = round(cw.get("temperature", 0))
    wind_speed = round(cw.get("windspeed", 0) / 3.6, 1)
    wind_angle = cw.get("winddirection", 0)

    curr_time = cw.get("time", "")
    h_idx = 0
    if "time" in hourly and curr_time in hourly["time"]:
        h_idx = hourly["time"].index(curr_time)

    humidity = hourly.get("relativehumidity_2m", [50])[h_idx]
    pressure_hpa = hourly.get("surface_pressure", [1013])[h_idx]
    pressure_mm = round(pressure_hpa * 0.750062)

    wcode = cw.get("weathercode", 0)
    is_day = cw.get("is_day", 1)
    suf = "_d" if is_day else "_n"
    if wcode == 0:
        icon = "skc" + suf
    elif wcode in [1, 2]:
        icon = "bkn" + suf
    elif wcode == 3:
        icon = "ovc"
    elif wcode in [45, 48]:
        icon = "fg"
    elif wcode in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
        icon = "ra"
    elif wcode in [71, 73, 75, 85, 86]:
        icon = "sn"
    elif wcode in [95, 96, 99]:
        icon = "ts"
    else:
        icon = "bkn" + suf

    sunrise = "06:00"
    sunset = "19:00"
    if daily.get("sunrise"):
        sunrise = daily["sunrise"][0].split("T")[-1][:5]
    if daily.get("sunset"):
        sunset = daily["sunset"][0].split("T")[-1][:5]

    hours_list = []
    temps = hourly.get("temperature_2m", [temp] * 24)
    for h in range(min(24, len(temps))):
        hours_list.append({"hour": str(h), "temp": round(temps[h])})

    max_t = round(daily.get("temperature_2m_max", [temp])[0])
    min_t = round(daily.get("temperature_2m_min", [temp])[0])

    parts = {
        "morning": {"temp_avg": round(temps[8] if len(temps) > 8 else temp), "icon": "bkn_d", "wind_speed": 2.5, "wind_angle": 180},
        "day": {"temp_avg": max_t, "icon": "bkn_d", "wind_speed": 3.0, "wind_angle": 180},
        "evening": {"temp_avg": round(temps[20] if len(temps) > 20 else temp), "icon": "bkn_n", "wind_speed": 2.0, "wind_angle": 170},
        "night": {"temp_avg": min_t, "icon": "skc_n", "wind_speed": 1.5, "wind_angle": 160}
    }

    return {
        "fact": {
            "temp": temp,
            "icon": icon,
            "wind_speed": wind_speed,
            "wind_angle": wind_angle,
            "humidity": humidity,
            "pressure_mm": pressure_mm
        },
        "forecasts": [
            {
                "sunrise": sunrise,
                "sunset": sunset,
                "moon_code": 9,
                "hours": hours_list,
                "parts": parts
            },
            {
                "parts": parts
            }
        ]
    }


def fetch_url_via(url, proxy, timeout, headers=None):
    """GET с опциональным HTTP-прокси, возвращает decoded text"""
    hdrs = {"User-Agent": "WeatherInformerLocal/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _wwo_icon(code):
    """WWO weatherCode -> имя иконки информера (без суффикса _d/_n)"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = 116
    if c == 113:
        return "skc"
    if c in (200, 386, 389):
        return "ts"
    if c in (179, 182, 185, 227, 230, 320, 323, 325, 326, 328, 329, 331, 332,
             334, 335, 338, 350, 362, 365, 367, 368, 371, 374, 377):
        return "sn"
    if c in (176, 263, 266, 281, 284, 293, 296, 299, 302, 305, 308, 353, 356,
             359):
        return "ra"
    if c in (143, 248, 260):
        return "fg"
    if c in (119, 122):
        return "ovc"
    if c == 116:
        return "bkn"
    return "bkn"


def _fmt12to24(s):
    """'05:48 AM' -> '05:48'"""
    try:
        t = datetime.strptime(s.strip(), "%I:%M %p")
        return t.strftime("%H:%M")
    except (ValueError, AttributeError):
        return (s or "")[:5]


def fetch_from_wttr(lat, lon):
    """Fallback-источник wttr.in (j1 JSON, без ключа) -> формат Яндекс.Погоды"""
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    wt = json.loads(fetch_url_via(url, None, 25))

    cc = (wt.get("current_condition") or [{}])[0]
    days = wt.get("weather") or []
    today = days[0] if days else {}
    hourly = today.get("hourly") or []

    temp = int(cc.get("temp_C", 0) or 0)
    humidity = int(cc.get("humidity", 50) or 50)
    pressure_hpa = int(cc.get("pressure", 1013) or 1013)
    pressure_mm = round(pressure_hpa * 0.750062)
    wind_speed = round(int(cc.get("windspeedKmph", 0) or 0) / 3.6, 1)
    wind_angle = int(cc.get("winddirDegree", 0) or 0)

    hour_now = datetime.now().hour
    suf = "_d" if 8 <= hour_now < 20 else "_n"
    icon = _wwo_icon(cc.get("weatherCode", "116")) + suf

    astro = (today.get("astronomy") or [{}])[0]
    sunrise = _fmt12to24(astro.get("sunrise", "06:00 AM"))
    sunset = _fmt12to24(astro.get("sunset", "19:00 PM"))

    hours_list = []
    for h in range(24):
        b = hourly[min(7, h // 3)] if hourly else {}
        hours_list.append({"hour": str(h), "temp": int(b.get("tempC", temp) or temp)})

    def block_part(idx, fallback_icon):
        b = hourly[idx] if len(hourly) > idx else {}
        hh = int(b.get("time", "0") or 0) // 100
        s2 = "_d" if 8 <= hh < 20 else "_n"
        return {
            "temp_avg": int(b.get("tempC", temp) or temp),
            "icon": _wwo_icon(b.get("weatherCode", "116")) + s2,
            "wind_speed": round(int(b.get("windspeedKmph", 5) or 5) / 3.6, 1),
            "wind_angle": int(b.get("winddirDegree", 180) or 180)
        }

    parts = {
        "morning": block_part(3, "bkn_d"),
        "day": block_part(4, "bkn_d"),
        "evening": block_part(6, "bkn_n"),
        "night": block_part(0, "skc_n")
    }

    return {
        "fact": {
            "temp": temp,
            "icon": icon,
            "wind_speed": wind_speed,
            "wind_angle": wind_angle,
            "humidity": humidity,
            "pressure_mm": pressure_mm
        },
        "forecasts": [
            {
                "sunrise": sunrise,
                "sunset": sunset,
                "moon_code": 9,
                "hours": hours_list,
                "parts": parts
            },
            {
                "parts": parts
            }
        ]
    }


_7TIMER_BFT_MS = [0, 0.8, 2.4, 4.3, 6.7, 9.3, 12.3, 15.5, 18.9, 22.6, 26.4,
                  30.5, 34.7]
_7TIMER_DIR_DEG = {"N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90, "ESE": 112,
                   "SE": 135, "SSE": 157, "S": 180, "SSW": 202, "SW": 225,
                   "WSW": 247, "W": 270, "WNW": 292, "NW": 315, "WNW2": 315,
                   "NNW": 337}


def fetch_from_7timer(lat, lon):
    """Last-resort источник 7timer civil (JSON, без ключа) -> формат Яндекса"""
    url = (f"https://www.7timer.info/bin/civil.php?lon={lon}&lat={lat}"
           f"&ac=0&unit=metric&output=json&tzshift=0")
    st = json.loads(fetch_url_via(url, None, 25))
    ds = st.get("dataseries") or []
    init = st.get("init", "")
    init_hour = int(init[8:10]) if len(init) >= 10 else 12

    def block_at(local_hour):
        best, best_delta = None, 999
        for b in ds:
            tp = int(b.get("timepoint", 0) or 0)
            h = (init_hour + tp) % 24
            d = min(abs(h - local_hour), 24 - abs(h - local_hour))
            if d < best_delta:
                best, best_delta = b, d
        return best or (ds[0] if ds else {})

    def b_icon(b):
        cc_pct = int(b.get("cloudcover", 50) or 0)
        prec = (b.get("prec_type", "none") or "none").lower()
        if prec in ("rain", "frzr"):
            return "ra"
        if prec in ("snow", "icep"):
            return "sn"
        if cc_pct < 15:
            return "skc"
        if cc_pct < 70:
            return "bkn"
        return "ovc"

    def b_part(b, fallback_icon):
        if not b:
            return {"temp_avg": 0, "icon": fallback_icon, "wind_speed": 2.0,
                    "wind_angle": 180}
        bft = int(b.get("wind10m", {}).get("speed", 2) or 2)
        direction = (b.get("wind10m", {}).get("direction", "S") or "S")[:3]
        return {
            "temp_avg": int(b.get("temp2m", 0) or 0),
            "icon": b_icon(b),
            "wind_speed": _7TIMER_BFT_MS[min(12, bft)],
            "wind_angle": _7TIMER_DIR_DEG.get(direction, 180)
        }

    b0 = block_at(datetime.now().hour)
    temp = int(b0.get("temp2m", 0) or 0)
    rh = b0.get("rh2m", 50)
    if isinstance(rh, str):
        digits = "".join(ch for ch in rh if ch.isdigit())
        rh = int(digits) if digits else 50
    hour_now = datetime.now().hour
    suf = "_d" if 8 <= hour_now < 20 else "_n"

    def b_fact_icon(b):
        i = b_icon(b)
        return i + suf

    parts = {
        "morning": b_part(block_at(9), "bkn_d"),
        "day": b_part(block_at(13), "bkn_d"),
        "evening": b_part(block_at(18), "bkn_n"),
        "night": b_part(block_at(1), "skc_n")
    }

    return {
        "fact": {
            "temp": temp,
            "icon": b_fact_icon(b0),
            "wind_speed": b_part(b0, "bkn_d")["wind_speed"],
            "wind_angle": b_part(b0, "bkn_d")["wind_angle"],
            "humidity": int(rh),
            "pressure_mm": 748
        },
        "forecasts": [
            {
                "sunrise": "06:00",
                "sunset": "19:00",
                "moon_code": 9,
                "hours": [{"hour": str(h), "temp": int(block_at(h).get("temp2m", temp) or temp)} for h in range(24)],
                "parts": parts
            },
            {
                "parts": parts
            }
        ]
    }


def fetch_weather(force=False):
    if force:
        with fetch_lock:
            return _fetch_weather_locked(True)
    # Неблокирующий лок: если другой поток уже обновляет — сразу отдаём кэш,
    # чтобы запросы планшетов не висели 20-60 с на цепочке фоллбеков
    if not fetch_lock.acquire(blocking=False):
        return cached_data
    try:
        return _fetch_weather_locked(False)
    finally:
        fetch_lock.release()


def _fetch_weather_locked(force=False):
    global cached_data, last_fetch_time, last_error_message
    cfg = load_config()
    interval = cfg.get("cache_interval_minutes", 50) * 60
    now = time.time()

    if not force and cached_data and (now - last_fetch_time < interval):
        return cached_data

    lat = cfg.get("lat", 56.317722)
    lon = cfg.get("lon", 43.999303)
    api_key = cfg.get("api", "")

    yandex_url = f"https://api.weather.yandex.ru/v2/forecast?lat={lat}&lon={lon}"
    headers = {
        "X-Yandex-Weather-Key": api_key,
        "User-Agent": "WeatherInformerLocal/1.0"
    }

    log.info("Requesting Yandex Weather API (%s, %s)...", lat, lon)
    req = urllib.request.Request(yandex_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "fact" in data and "forecasts" in data:
                data = normalize_weather_data(data)
                cached_data = data
                last_fetch_time = time.time()
                last_error_message = None
                save_disk_cache(data)
                stats_log_source("Yandex", data)
                log.info("Successfully fetched and cached Yandex weather (temp: %s°)", data["fact"].get("temp"))
                return cached_data
    except urllib.error.HTTPError as e:
        last_error_message = f"Yandex HTTP {e.code}: {e.reason}"
        log.warning("Yandex API returned HTTP %s: %s", e.code, e.reason)
    except Exception as e:
        last_error_message = f"Yandex request error: {e}"
        log.warning("Yandex request failed: %s", e)

    # Fallback на Open-Meteo при ошибке Яндекса (опционально через прокси)
    if cfg.get("enable_openmeteo_fallback", True):
        proxy = cfg.get("om_proxy", "") or None
        log.info("Attempting fallback to Open-Meteo%s...",
                 " via proxy" if proxy else "")
        try:
            om_data = fetch_from_openmeteo(lat, lon, proxy)
            cached_data = om_data
            last_fetch_time = time.time()
            last_error_message = "Active fallback: Open-Meteo (Yandex quota/error)"
            save_disk_cache(om_data)
            stats_log_source("Open-Meteo", om_data)
            log.info("Successfully updated weather via Open-Meteo fallback (temp: %s°)", om_data["fact"].get("temp"))
            return cached_data
        except Exception as e:
            log.error("Open-Meteo fallback also failed: %s", e)

    # Fallback 2: wttr.in (без ключа)
    if cfg.get("enable_wttr_fallback", True):
        log.info("Attempting fallback to wttr.in...")
        try:
            wt_data = fetch_from_wttr(lat, lon)
            cached_data = wt_data
            last_fetch_time = time.time()
            last_error_message = "Active fallback: wttr.in (Yandex/OM unavailable)"
            save_disk_cache(wt_data)
            stats_log_source("wttr.in", wt_data)
            log.info("Successfully updated weather via wttr.in fallback (temp: %s°)", wt_data["fact"].get("temp"))
            return cached_data
        except Exception as e:
            log.error("wttr.in fallback also failed: %s", e)

    # Fallback 3: 7timer (последний рубеж)
    if cfg.get("enable_7timer_fallback", True):
        log.info("Attempting fallback to 7timer...")
        try:
            st_data = fetch_from_7timer(lat, lon)
            cached_data = st_data
            last_fetch_time = time.time()
            last_error_message = "Active fallback: 7timer (others unavailable)"
            save_disk_cache(st_data)
            stats_log_source("7timer", st_data)
            log.info("Successfully updated weather via 7timer fallback (temp: %s°)", st_data["fact"].get("temp"))
            return cached_data
        except Exception as e:
            log.error("7timer fallback also failed: %s", e)

    # Тотальный фейл всех источников: считаем попытку израсходованной,
    # чтобы не долбить Яндекс/фолбэки на каждом тике (60 с) и каждом
    # запросе планшета — следующая попытка только через интервал кэша
    last_fetch_time = time.time()
    return cached_data


class WeatherHTTPHandler(BaseHTTPRequestHandler):
    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Yandex-Weather-Key, Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ["/health", "/status"]:
            now = time.time()
            age_min = (now - last_fetch_time) / 60 if last_fetch_time else -1
            status_body = json.dumps({
                "status": "ok" if cached_data else "no_cache",
                "cache_age_minutes": round(age_min, 1),
                "last_error": last_error_message,
                "has_weather_data": cached_data is not None
            }, indent=2)
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(status_body.encode("utf-8"))
            return

        # Любой погодный маршрут: /, /weather.json, /forecast.json, /v2/forecast
        data = fetch_weather(force=False)
        if data:
            body = json.dumps(data, ensure_ascii=False)
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        else:
            self.send_response(503)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            err = json.dumps({"error": "Weather data unavailable", "details": last_error_message})
            self.wfile.write(err.encode("utf-8"))

    def log_message(self, format, *args):
        # Компактный лог HTTP-запросов
        log.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)


def background_refresher():
    """Фоновый поток для планового обновления кэша"""
    while True:
        try:
            fetch_weather(force=False)
        except Exception as e:
            log.error("Background refresher error: %s", e)
        time.sleep(60)


def main():
    load_disk_cache()
    cfg = load_config()
    port = int(cfg.get("port", 8085))

    # Биндим порт ДО старта фоновых потоков: вторая копия сервера умирает
    # здесь же мгновенно — без потоков и без запросов к источникам
    # (защита от дублей: ручные запуски поверх systemd-инстанса)
    server = HTTPServer(("0.0.0.0", port), WeatherHTTPHandler)
    log.info("WeatherInformer Caching Proxy listening on port %d...", port)

    # Первичный запрос в фоне
    t = threading.Thread(target=background_refresher, daemon=True)
    t.start()

    # Сбор статистики по всем источникам (CSV)
    if load_config().get("stats_enabled", True):
        ts_thread = threading.Thread(target=stats_loop, daemon=True)
        ts_thread.start()
        log.info("Weather stats collector enabled -> %s", STATS_FILE)

    log.info("  http://<IP>:%d/weather.json", port)
    log.info("  http://<IP>:%d/health", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
