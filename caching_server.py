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
import urllib.parse
from urllib.parse import parse_qs, urlparse

from gismeteo_provider import GismeteoProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("WeatherCache")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, "weather_cache.json")

# Кэш погоды по точкам (выбор города на планшете): ключ "lat:lon" с точностью 0.01°
LOCATION_CACHE = {}
KEY_LOCKS = {}
MAX_LOCATIONS = 8
NOMINATIM = "https://nominatim.openstreetmap.org"
GEO_HEADERS = {"User-Agent": "WeatherInformerLocal/1.0 (lan weather kiosk)"}


def loc_key(lat, lon):
    return f"{round(float(lat), 2)}:{round(float(lon), 2)}"


def _nominatim_get(path, params, timeout=8):
    """Запрос к Nominatim (геокодинг): напрямую, при неудаче — через прокси из конфига."""
    cfg = load_config()
    proxy = cfg.get("om_proxy", "") or None
    url = NOMINATIM + path + "?" + urllib.parse.urlencode(params)
    last_exc = None
    for use_proxy in (None, proxy):
        try:
            return json.loads(fetch_url_via(url, use_proxy, timeout, headers=GEO_HEADERS))
        except Exception as e:
            last_exc = e
    raise last_exc if last_exc else RuntimeError("nominatim unavailable")


def geocode_search(q, limit=6):
    """Поиск города по названию -> [{name, display, lat, lon}]"""
    res = _nominatim_get("/search", {
        "q": q, "format": "jsonv2", "limit": limit,
        "accept-language": "ru", "addressdetails": 0})
    out = []
    for it in res[:limit]:
        try:
            name = (it.get("name") or "").strip() or it.get("display_name", "").split(",")[0].strip()
            out.append({
                "name": name,
                "display": it.get("display_name", ""),
                "lat": round(float(it["lat"]), 4),
                "lon": round(float(it["lon"]), 4)})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def reverse_geocode(lat, lon):
    """Координаты -> имя города"""
    res = _nominatim_get("/reverse", {
        "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
        "accept-language": "ru", "addressdetails": 1})
    addr = res.get("address", {}) or {}
    name = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("state")
            or res.get("name") or "").strip()
    if not name:
        name = (res.get("display_name", "").split(",")[0] or "").strip()
    return {"name": name, "lat": round(float(lat), 4), "lon": round(float(lon), 4)}


GISMETEO = GismeteoProvider(
    geocode_fn=geocode_search,
    city_cache_path=os.path.join(SCRIPT_DIR, "gismeteo_cities.json"),
    logger=log,
)
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
    "enable_gismeteo_fallback": True,
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
                log.debug("Loaded config from %s", CONFIG_FILE)
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
                fact["pressure_mm"] = None
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
                    cfg0 = load_config()
                    dk = loc_key(cfg0.get("lat", 56.317722), cfg0.get("lon", 43.999303))
                    LOCATION_CACHE[dk] = {"data": cached_data, "ts": last_fetch_time}
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



def fetch_from_openweathermap(lat, lon, api_key):
    """Источник OpenWeatherMap (нужен ключ) -> формат Яндекса"""
    def _get(path):
        url = (f"https://api.openweathermap.org/data/2.5/{path}"
               f"?lat={lat}&lon={lon}&units=metric&appid={api_key}")
        req = urllib.request.Request(url, headers={"User-Agent": "WeatherInformerLocal/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    cur = _get("weather")
    fc = _get("forecast")

    def _owm_base(code):
        if 200 <= code < 300:
            return "ovc_ts"
        if 300 <= code < 400:
            return "ovc_ra"
        if 500 <= code < 600:
            return "ovc_ra"
        if 600 <= code < 700:
            return "ovc_sn"
        if 700 <= code < 800:
            return "ovc"
        if code == 800:
            return "skc"
        if code in (801, 802):
            return "bkn"
        return "ovc"

    def _owm_icon(weather_entry):
        base = _owm_base(int(weather_entry.get("id", 800)))
        day = str(weather_entry.get("icon", "01d"))[-1] == "d"
        if base in ("skc", "bkn"):
            return base + ("_d" if day else "_n")
        return base

    w0 = (cur.get("weather") or [{}])[0]
    main = cur.get("main", {})
    wind = cur.get("wind", {})
    sys = cur.get("sys", {})
    temp = round(main.get("temp", 0))
    wind_speed = round(wind.get("speed", 0), 1)
    wind_angle = int(wind["deg"]) if wind.get("deg") is not None else None
    humidity = int(main.get("humidity", 50) or 50)
    pressure_mm = round(int(main.get("pressure", 1013) or 1013) * 0.750062)
    fact_icon = _owm_icon(w0)
    sunrise_dt = datetime.fromtimestamp(sys.get("sunrise", 0))
    sunset_dt = datetime.fromtimestamp(sys.get("sunset", 0))
    sunrise = sunrise_dt.strftime("%H:%M")
    sunset = sunset_dt.strftime("%H:%M")

    # Части суток и часы из 3-часового прогноза
    # dt_txt у OWM в UTC — приводим к локальному времени по сдвигу города
    tz_shift = int((fc.get("city") or {}).get("timezone", 0))
    blocks = []
    for it in fc.get("list", []):
        try:
            dt_l = datetime.fromisoformat(it.get("dt_txt", "")) + timedelta(seconds=tz_shift)
        except (ValueError, TypeError):
            continue
        blocks.append({
            "date": dt_l.strftime("%Y-%m-%d"), "hour": dt_l.hour,
            "temp": round(it.get("main", {}).get("temp", 0)),
            "humidity": int(it.get("main", {}).get("humidity", 50) or 50),
            "wind_speed": round(it.get("wind", {}).get("speed", 0), 1),
            "wind_angle": (int(it["wind"]["deg"]) if it.get("wind", {}).get("deg") is not None else None),
            "icon": _owm_icon((it.get("weather") or [{}])[0])
        })
    today = blocks[0]["date"] if blocks else ""
    morning = [b for b in blocks if b["date"] == today and 6 <= b["hour"] < 12]
    dayb = [b for b in blocks if b["date"] == today and 12 <= b["hour"] < 18]
    evening = [b for b in blocks if b["date"] == today and 18 <= b["hour"] < 24]
    night = [b for b in blocks if b["date"] != today and b["hour"] < 6]

    def avg_block(bs, fallback_icon):
        if not bs:
            return {"temp_avg": temp, "icon": fallback_icon, "wind_speed": wind_speed, "wind_angle": wind_angle}
        temps = [b["temp"] for b in bs]
        mid = bs[len(bs) // 2]
        return {
            "temp_avg": round(sum(temps) / len(temps)),
            "icon": mid["icon"],
            "wind_speed": round(sum(b["wind_speed"] for b in bs) / len(bs), 1),
            "wind_angle": mid["wind_angle"]
        }

    parts = {
        "morning": avg_block(morning, "bkn_d"),
        "day": avg_block(dayb, "bkn_d"),
        "evening": avg_block(evening, "bkn_n"),
        "night": avg_block(night, "skc_n")
    }

    hours_list = [{"hour": str(b["hour"]), "temp": b["temp"]} for b in blocks if b["date"] == today]

    return {
        "fact": {
            "temp": temp,
            "icon": fact_icon,
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

    def _om_icon(wcode, is_day):
        suf = "_d" if is_day else "_n"
        if wcode == 0:
            return "skc" + suf
        if wcode in [1, 2]:
            return "bkn" + suf
        if wcode == 3:
            return "ovc"
        if wcode in [45, 48]:
            return "ovc"
        if wcode in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
            return "ovc_ra"
        if wcode in [71, 73, 75, 85, 86]:
            return "ovc_sn"
        if wcode in [95, 96, 99]:
            return "ovc_ts"
        return "bkn" + suf

    wcode = cw.get("weathercode", 0)
    is_day = cw.get("is_day", 1)
    icon = _om_icon(wcode, is_day)

    sunrise = "06:00"
    sunset = "19:00"
    if daily.get("sunrise"):
        sunrise = daily["sunrise"][0].split("T")[-1][:5]
    if daily.get("sunset"):
        sunset = daily["sunset"][0].split("T")[-1][:5]

    def _hm(v):
        a = str(v).split(":")
        return int(a[0]) * 60 + int(a[1])

    sr_m, ss_m = _hm(sunrise), _hm(sunset)
    times = hourly.get("time", [])
    wcodes = hourly.get("weathercode", [])

    def part_icon(h):
        """Значок части суток из фактического погодного кода этого часа"""
        wc = 0
        for i, tstr in enumerate(times):
            try:
                if int(tstr[11:13]) == h and i < len(wcodes):
                    wc = wcodes[i]
                    break
            except (ValueError, IndexError):
                pass
        return _om_icon(wc, 1 if sr_m <= h * 60 < ss_m else 0)

    hours_list = []
    temps = hourly.get("temperature_2m", [temp] * 24)
    for h in range(min(24, len(temps))):
        hours_list.append({"hour": str(h), "temp": round(temps[h])})

    max_t = round(daily.get("temperature_2m_max", [temp])[0])
    min_t = round(daily.get("temperature_2m_min", [temp])[0])

    def part_wind(h0, h1):
        """Средний ветер по часам [h0, h1) из hourly; None если данных нет"""
        vals = hourly.get("windspeed_10m", []) or []
        dirs = hourly.get("winddirection_10m", []) or []
        picked = []
        for i, tstr in enumerate(times):
            try:
                if h0 <= int(tstr[11:13]) < h1 and i < len(vals) and vals[i] is not None:
                    picked.append((vals[i], dirs[i] if i < len(dirs) and dirs[i] is not None else None))
            except (ValueError, IndexError):
                continue
        if not picked:
            return None, None
        avg_dir = [d for _, d in picked if d is not None]
        return round(sum(v for v, _ in picked) / len(picked) / 3.6, 1), \
            (round(sum(avg_dir) / len(avg_dir)) if avg_dir else None)

    def _pw(h0, h1):
        w, a = part_wind(h0, h1)
        return {"wind_speed": w, "wind_angle": a}

    parts = {
        "morning": dict({"temp_avg": round(temps[8] if len(temps) > 8 else temp), "icon": part_icon(9)}, **_pw(6, 12)),
        "day": dict({"temp_avg": max_t, "icon": part_icon(13)}, **_pw(12, 18)),
        "evening": dict({"temp_avg": round(temps[20] if len(temps) > 20 else temp), "icon": part_icon(19)}, **_pw(18, 24)),
        "night": dict({"temp_avg": min_t, "icon": part_icon(2)}, **_pw(0, 6))
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


def _icon_with_suffix(base, is_day):
    """skc/bkn получают суффикс _d/_n; конечные классы (ovc_ra и пр.) — как есть"""
    if base in ("skc", "bkn"):
        return base + ("_d" if is_day else "_n")
    return base


def _wwo_icon(code):
    """WWO weatherCode -> имя иконки информера (без суффикса _d/_n)"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = 116
    if c == 113:
        return "skc"
    if c in (200, 386, 389):
        return "ovc_ts"
    if c in (179, 182, 185, 227, 230, 320, 323, 325, 326, 328, 329, 331, 332,
             334, 335, 338, 350, 362, 365, 367, 368, 371, 374, 377):
        return "ovc_sn"
    if c in (176, 263, 266, 281, 284, 293, 296, 299, 302, 305, 308, 353, 356,
             359):
        return "ovc_ra"
    if c in (143, 248, 260):
        return "ovc"
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
    icon = _icon_with_suffix(_wwo_icon(cc.get("weatherCode", "116")), 8 <= hour_now < 20)

    astro = (today.get("astronomy") or [{}])[0]
    sunrise = _fmt12to24(astro["sunrise"]) if astro.get("sunrise") else None
    sunset = _fmt12to24(astro["sunset"]) if astro.get("sunset") else None

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
            "icon": _icon_with_suffix(_wwo_icon(b.get("weatherCode", "116")), 8 <= hh < 20),
            "wind_speed": round(int(b.get("windspeedKmph", 5) or 5) / 3.6, 1),
            "wind_angle": (int(b["winddirDegree"]) if b.get("winddirDegree") is not None else None)
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
            return "ovc_ra"
        if prec in ("snow", "icep"):
            return "ovc_sn"
        if cc_pct < 15:
            return "skc"
        if cc_pct < 70:
            return "bkn"
        return "ovc"

    def b_part(b, fallback_icon, hour=12):
        if not b:
            return {"temp_avg": 0, "icon": fallback_icon, "wind_speed": 2.0,
                    "wind_angle": 180}
        bft = int(b.get("wind10m", {}).get("speed", 2) or 2)
        direction = (b.get("wind10m", {}).get("direction", "S") or "S")[:3]
        return {
            "temp_avg": int(b.get("temp2m", 0) or 0),
            "icon": _icon_with_suffix(b_icon(b), 6 <= hour < 18),
            "wind_speed": _7TIMER_BFT_MS[min(12, bft)],
            "wind_angle": _7TIMER_DIR_DEG.get(direction)
        }

    b0 = block_at(datetime.now().hour)
    temp = int(b0.get("temp2m", 0) or 0)
    rh = b0.get("rh2m")
    if isinstance(rh, str):
        digits = "".join(ch for ch in rh if ch.isdigit())
        rh = int(digits) if digits else None
    if rh is None:
        rh = None
    hour_now = datetime.now().hour
    suf = "_d" if 8 <= hour_now < 20 else "_n"

    def b_fact_icon(b):
        return _icon_with_suffix(b_icon(b), 8 <= hour_now < 20)

    parts = {
        "morning": b_part(block_at(9), "bkn_d", 9),
        "day": b_part(block_at(13), "bkn_d", 13),
        "evening": b_part(block_at(18), "bkn_n", 18),
        "night": b_part(block_at(1), "skc_n", 1)
    }

    return {
        "fact": {
            "temp": temp,
            "icon": b_fact_icon(b0),
            "wind_speed": b_part(b0, "bkn_d")["wind_speed"],
            "wind_angle": b_part(b0, "bkn_d")["wind_angle"],
            "humidity": (int(rh) if rh is not None else None),
            "pressure_mm": None
        },
        "forecasts": [
            {
                "sunrise": None,
                "sunset": None,
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
    """Обновление погоды для города по умолчанию (фоновый цикл)"""
    cfg = load_config()
    return get_weather_for(cfg.get("lat", 56.317722), cfg.get("lon", 43.999303), force=force)


def get_weather_for(lat, lon, force=False, sources=None):
    """Погода для конкретной точки: свежий кэш — мгновенно,
    устаревший — фетч с неблокирующим локом на точку.
    sources: None (вся цепочка) или кортеж имён источников-фильтр."""
    key = loc_key(lat, lon) + (":" + ",".join(sorted(sources)) if sources else "")
    interval = load_config().get("cache_interval_minutes", 90) * 60
    entry = LOCATION_CACHE.get(key)
    if not force and entry and entry.get("data") and (time.time() - entry["ts"] < interval):
        return entry["data"]
    lock = KEY_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        # другой поток уже обновляет эту точку — отдаём что есть (может быть None)
        return entry["data"] if entry else None
    try:
        return _fetch_weather_locked(force, lat, lon, sources, key)
    finally:
        lock.release()


def _store_location_result(key, data, note=None):
    """Сохранение результата фетча; для города по умолчанию — ещё глобальный кэш и диск."""
    global cached_data, last_fetch_time, last_error_message
    ts = time.time()
    LOCATION_CACHE[key] = {"data": data, "ts": ts}
    cfg = load_config()
    defk = loc_key(cfg.get("lat", 56.317722), cfg.get("lon", 43.999303))
    if len(LOCATION_CACHE) > MAX_LOCATIONS:
        for k in sorted(LOCATION_CACHE, key=lambda k: LOCATION_CACHE[k]["ts"])[:-MAX_LOCATIONS]:
            if k != key and k != defk:  # дефолтный город не эвиктим — иначе внеплановый расход квоты
                LOCATION_CACHE.pop(k, None)
    if key == defk:
        cached_data = data
        last_fetch_time = ts
        last_error_message = note
        save_disk_cache(data)


def _fetch_weather_locked(force, lat, lon, sources=None, key=None):
    global last_error_message
    if key is None:
        key = loc_key(lat, lon)
    interval = load_config().get("cache_interval_minutes", 90) * 60
    now = time.time()
    entry = LOCATION_CACHE.get(key)
    if not force and entry and entry.get("data") and (now - entry["ts"] < interval):
        return entry["data"]

    def want(name):
        return sources is None or name in sources

    api_key = load_config().get("api", "")

    if want("yandex"):
        yandex_url = f"https://api.weather.yandex.ru/v2/forecast?lat={lat}&lon={lon}"
        headers = {
            "X-Yandex-Weather-Key": api_key,
            "User-Agent": "WeatherInformerLocal/1.0"
        }

        log.info("Requesting Yandex Weather API (%s, %s) [%s]...", lat, lon, key)
        req = urllib.request.Request(yandex_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "fact" in data and "forecasts" in data:
                    data = normalize_weather_data(data)
                    data["src"] = "Yandex"
                    _store_location_result(key, data, None)
                    stats_log_source("Yandex", data)
                    log.info("Successfully fetched and cached Yandex weather (temp: %s°)", data["fact"].get("temp"))
                    return data
        except urllib.error.HTTPError as e:
            last_error_message = f"Yandex HTTP {e.code}: {e.reason}"
            log.warning("Yandex API returned HTTP %s: %s", e.code, e.reason)
        except Exception as e:
            last_error_message = f"Yandex request error: {e}"
            log.warning("Yandex request failed: %s", e)

    # Fallback на OpenWeatherMap (если задан ключ) при ошибке Яндекса
    if want("owm") and load_config().get("enable_openweathermap_fallback", True) and load_config().get("openweathermap_api_key", ""):
        owm_key = load_config().get("openweathermap_api_key", "")
        log.info("Attempting fallback to OpenWeatherMap...")
        try:
            owm_data = fetch_from_openweathermap(lat, lon, owm_key)
            owm_data["src"] = "OpenWeatherMap"
            _store_location_result(key, owm_data, "Active fallback: OpenWeatherMap (Yandex quota/error)")
            stats_log_source("OpenWeatherMap", owm_data)
            log.info("Successfully updated weather via OpenWeatherMap fallback (temp: %s°)", owm_data["fact"].get("temp"))
            return owm_data
        except Exception as e:
            log.error("OpenWeatherMap fallback failed: %s", e)

    # Fallback на Open-Meteo при ошибке Яндекса (опционально через прокси)
    if want("om") and load_config().get("enable_openmeteo_fallback", True):
        proxy = load_config().get("om_proxy", "") or None
        log.info("Attempting fallback to Open-Meteo%s...",
                 " via proxy" if proxy else "")
        try:
            om_data = fetch_from_openmeteo(lat, lon, proxy)
            om_data["src"] = "Open-Meteo"
            _store_location_result(key, om_data, "Active fallback: Open-Meteo (Yandex quota/error)")
            stats_log_source("Open-Meteo", om_data)
            log.info("Successfully updated weather via Open-Meteo fallback (temp: %s°)", om_data["fact"].get("temp"))
            return om_data
        except Exception as e:
            log.error("Open-Meteo fallback also failed: %s", e)

    # Fallback: Gismeteo (без токена, изолированный провайдер)
    if want("gismeteo") and load_config().get("enable_gismeteo_fallback", True):
        log.info("Attempting fallback to Gismeteo...")
        try:
            g_data = GISMETEO.get_weather(latitude=lat, longitude=lon)
            _store_location_result(key, g_data, "Active fallback: Gismeteo (Yandex/OWM/OM unavailable)")
            stats_log_source("Gismeteo", g_data)
            log.info("Successfully updated weather via Gismeteo fallback (temp: %s°)", g_data["fact"].get("temp"))
            return g_data
        except Exception as e:
            last_error_message = f"Gismeteo: {e}"
            log.error("Gismeteo fallback failed: %s", e)

    # Fallback 2: wttr.in (без ключа)
    if want("wttr") and load_config().get("enable_wttr_fallback", True):
        log.info("Attempting fallback to wttr.in...")
        try:
            wt_data = fetch_from_wttr(lat, lon)
            wt_data["src"] = "wttr.in"
            _store_location_result(key, wt_data, "Active fallback: wttr.in (Yandex/OM unavailable)")
            stats_log_source("wttr.in", wt_data)
            log.info("Successfully updated weather via wttr.in fallback (temp: %s°)", wt_data["fact"].get("temp"))
            return wt_data
        except Exception as e:
            log.error("wttr.in fallback also failed: %s", e)

    # Fallback 3: 7timer (последний рубеж)
    if want("7timer") and load_config().get("enable_7timer_fallback", True):
        log.info("Attempting fallback to 7timer...")
        try:
            st_data = fetch_from_7timer(lat, lon)
            st_data["src"] = "7timer"
            _store_location_result(key, st_data, "Active fallback: 7timer (others unavailable)")
            stats_log_source("7timer", st_data)
            log.info("Successfully updated weather via 7timer fallback (temp: %s°)", st_data["fact"].get("temp"))
            return st_data
        except Exception as e:
            log.error("7timer fallback also failed: %s", e)

    # Тотальный фейл всех источников: считаем попытку израсходованной,
    # чтобы не долбить источники на каждом тике; старые данные оставляем
    ts = time.time()
    if entry is not None:
        entry["ts"] = ts
    else:
        LOCATION_CACHE[key] = {"data": None, "ts": ts}
    cfg = load_config()
    defk = loc_key(cfg.get("lat", 56.317722), cfg.get("lon", 43.999303))
    if key == defk:
        global last_fetch_time
        last_fetch_time = ts
    return LOCATION_CACHE[key]["data"]


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

        if path == "/geocode":
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q", [""])[0] or "").strip()
            if len(q) < 2:
                self.send_response(400)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "query too short"}).encode("utf-8"))
                return
            try:
                results = geocode_search(q)
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(results, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log.warning("geocode failed: %s", e)
                self.send_response(502)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"geocode failed: {e}"}).encode("utf-8"))
            return

        if path == "/reverse":
            qs = parse_qs(urlparse(self.path).query)
            try:
                r_lat = float(qs["lat"][0])
                r_lon = float(qs["lon"][0])
            except (KeyError, ValueError, IndexError):
                self.send_response(400)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "lat/lon required"}).encode("utf-8"))
                return
            try:
                place = reverse_geocode(r_lat, r_lon)
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(place, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log.warning("reverse geocode failed: %s", e)
                self.send_response(502)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"reverse failed: {e}"}).encode("utf-8"))
            return

        # Любой погодный маршрут: /, /weather.json, /forecast.json, /v2/forecast
        # Координаты можно передать (?lat=&lon=) — выбор города на планшете;
        # без параметров отдаётся город по умолчанию из config.json
        qs = parse_qs(urlparse(self.path).query)
        lat_raw = qs.get("lat", [None])[0]
        lon_raw = qs.get("lon", [None])[0]
        if lat_raw is None and lon_raw is None:
            # координаты не переданы — город по умолчанию из config.json
            cfg_w = load_config()
            q_lat = cfg_w.get("lat", 56.317722)
            q_lon = cfg_w.get("lon", 43.999303)
        else:
            # координаты переданы (обе) — валидация обязательна
            try:
                q_lat = float(lat_raw)
                q_lon = float(lon_raw)
            except (TypeError, ValueError):
                q_lat = q_lon = None
            if q_lat is None or not (-90.0 <= q_lat <= 90.0 and -180.0 <= q_lon <= 180.0):
                self.send_response(400)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "invalid lat/lon"}).encode("utf-8"))
                log.warning("Rejected weather request with invalid coords: lat=%r lon=%r", lat_raw, lon_raw)
                return
        src_filter = (qs.get("source", [""])[0] or "").strip().lower()
        sources = ("gismeteo",) if src_filter == "gismeteo" else None
        data = get_weather_for(q_lat, q_lon, force=False, sources=sources)
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
