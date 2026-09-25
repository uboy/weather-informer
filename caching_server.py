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
import sqlite3
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import parse_qs, urlparse

from gismeteo_provider import GismeteoProvider
from foreca_provider import ForecaProvider

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


def moon_phase_code(dt=None):
    """Код фазы луны 0-7 (0 новолуние, 4 полнолуние) — семантика Яндекс.Погоды."""
    if dt is None:
        dt = datetime.now()
    ref = datetime(2000, 1, 6, 18, 14)  # опорное новолуние
    days = (dt - ref).total_seconds() / 86400.0
    frac = (days % 29.530588853) / 29.530588853
    return int(round(frac * 8)) % 8


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
FORECAST_DB = os.path.join(SCRIPT_DIR, "forecast.db")
HISTORY_DAYS = 90  # ротация истории: кольцо в 90 дней
last_obs_fetch = 0.0  # троттлинг /observations/fetch
STATS_LOCK = threading.Lock()

# Дефолтные параметры
DEFAULT_CONFIG = {
    "port": 8085,
    "cache_interval_minutes": 60,  # 24 запроса в сутки (квота 30/день)
    "api": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "lat": 56.317722,
    "lon": 43.999303,
    "enable_openmeteo_fallback": True,
    "om_proxy": "",
    "enable_gismeteo_fallback": True,
    "foreca_api_key": "",
    "enable_foreca_fallback": True,
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

FORECA = ForecaProvider(
    token=load_config().get("foreca_api_key", ""),
    geocode_fn=geocode_search,
    reverse_fn=reverse_geocode,
    location_cache_path=os.path.join(SCRIPT_DIR, "foreca_locations.json"),
    logger=log,
)



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


def _history_conn():
    conn = sqlite3.connect(FORECAST_DB)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS forecast_points (
        created TEXT NOT NULL, provider TEXT NOT NULL, loc TEXT NOT NULL, valid_at TEXT NOT NULL,
        temperature REAL, humidity REAL, pressure REAL, precip_prob REAL, precip_mm REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        ts TEXT NOT NULL, station TEXT NOT NULL, source TEXT NOT NULL,
        temperature REAL, humidity REAL, pressure REAL, precip_mm REAL,
        UNIQUE(ts, station, source))""")
    try:
        conn.execute("ALTER TABLE observations ADD COLUMN phys_station TEXT")
    except Exception:
        pass
    return conn


def record_forecast(provider, loc, data):
    """Прогноз в forecast.db: факт (valid=now) + почасовые точки активного дня.
    Поля, которых источник не отдаёт, пишутся как NULL (no fake data)."""
    try:
        now = datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        f = data.get("fact") or {}
        rows = [(now_iso, provider, loc, now_iso,
                 f.get("temp"), f.get("humidity"), f.get("pressure_mm"),
                 None, None)]
        hours = ((data.get("forecasts") or [{}])[0].get("hours")) or []
        cur_hour = now.hour
        for h in hours:
            try:
                hh = int(h.get("hour", -1))
            except (TypeError, ValueError):
                continue
            if hh < cur_hour:
                continue  # прошедшие часы активного дня — мусор для lead-метрик
            day = now.date()
            valid_at = f"{day.isoformat()}T{hh:02d}:00:00"
            rows.append((now_iso, provider, loc, valid_at,
                         h.get("temp"), None, None, None, None))
        # завтрашние часы (forecasts[1].hours есть у Яндекса) — оживляет lead 23-46ч
        f1_hours = ((data.get("forecasts") or [{}, {}])[1:2] or [{}])[0].get("hours") or []
        tomorrow = now.date() + timedelta(days=1)
        for h in f1_hours:
            try:
                hh = int(h.get("hour", -1))
            except (TypeError, ValueError):
                continue
            valid_at = f"{tomorrow.isoformat()}T{hh:02d}:00:00"
            rows.append((now_iso, provider, loc, valid_at,
                         h.get("temp"), None, None, None, None))
        conn = _history_conn()
        try:
            conn.executemany("INSERT INTO forecast_points VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("history: forecast not recorded: %s", e)


def rotate_stats_if_needed():
    """Ротация статистики-рингбуфером: файл старше 90 дней -> weather_stats.csv.1"""
    try:
        if not os.path.isfile(STATS_FILE):
            return
        # возраст по ПЕРВОЙ записи файла (mtime освежается каждым append)
        first_ts = None
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                f.readline()  # заголовок
                line = f.readline().strip()
            if line:
                first_ts = line.split(",")[0]
        except OSError:
            pass
        age_days = 0
        if first_ts:
            try:
                age_days = (datetime.now() - datetime.fromisoformat(first_ts)).total_seconds() / 86400.0
            except ValueError:
                age_days = 0
        if age_days > HISTORY_DAYS:
            old = STATS_FILE + ".1"
            if os.path.isfile(old):
                os.remove(old)
            os.replace(STATS_FILE, old)
            log.info("stats: ротация рингбуфера (%.0f дней) -> %s", age_days, old)
    except OSError as e:
        log.warning("stats: ротация не удалась: %s", e)


def prune_history():
    """Удаление записей истории старше HISTORY_DAYS."""
    try:
        conn = _history_conn()
        try:
            conn.execute("DELETE FROM forecast_points WHERE created < datetime('now', ?)",
                         (f"-{HISTORY_DAYS} days",))
            conn.execute("DELETE FROM observations WHERE ts < datetime('now', ?)",
                         (f"-{HISTORY_DAYS} days",))
            conn.execute("DELETE FROM synop_raw WHERE ts_utc < datetime('now', ?)",
                         (f"-{HISTORY_DAYS} days",))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("history: prune failed: %s", e)


def collect_foreca_observations():
    """Наблюдения ближайших станций Foreca -> observations.db (ground truth)."""
    if not load_config().get("foreca_api_key", ""):
        return 0
    try:
        cfg = load_config()
        lat, lon = cfg.get("lat", 56.317722), cfg.get("lon", 43.999303)
        obs_list = FORECA.get_observations(latitude=lat, longitude=lon, stations=6)
        conn = _history_conn()
        n = 0
        try:
            for o in obs_list:
                ts = o.get("time", "")
                if "T" in ts:
                    ts = ts[:19]  # нормализация naive локального времени
                conn.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?)",
                    (ts, o.get("station", ""), "foreca-obs",
                     o.get("temperature"), o.get("relHumidity"),
                     o.get("pressure"), o.get("precip_mm"),
                     OGIMET_PHYS.get(o.get("station", ""), o.get("station", ""))))
                n += 1
            conn.commit()
        finally:
            conn.close()
        log.info("observations: записано %d наблюдений Foreca", n)
        return n
    except Exception as e:
        log.warning("observations: Foreca недоступен: %s", e)
        return 0


OGIMET_PHYS = {  # Foreca-имя -> физическая станция
    "Niznij Novgorod": "27459", "Nizhny Novgorod": "27459",
    "Nizhny Novgorod/Strigino": "STRIGINO",
    "Volzskaja Gmo": "VOLGA_GMO", "Sergac": "SERGACH",
    "Krasnye Baki": "KRASNYE_BAKI",
}
SYNOP_GROUPS = {  # индикатор группы -> (поле, множитель)
    "1": ("temperature", 0.1), "3": ("pressure", 0.1), "4": ("pressure_sl", 0.1),
}


def _decode_synop(msg):
    """Минимальный FM-12 SYNOP декодер: температура (1sTTT), давление на станции
    (3P0P0P0), на уровне моря (4PPPP), осадки (6RRRtR). Давление кодируется
    без ведущих сотен: val<500 -> +1000 hPa, иначе +900 hPa (WMO)."""
    out = {"temperature": None, "pressure": None, "pressure_sl": None, "precip_mm": None}
    sec3 = False
    for tok in msg.split():
        if tok == "333":
            sec3 = True
            continue
        if tok in ("AAXX", "BBXX") or not tok:
            continue
        if tok[0] == "1" and len(tok) == 5 and tok[1] in "01" and tok[2:].isdigit() and not sec3:
            sign = -1.0 if tok[1] == "1" else 1.0
            out["temperature"] = sign * int(tok[2:]) / 10.0
        elif tok[0] == "3" and len(tok) == 5 and tok[1:].isdigit() and not sec3:
            val = int(tok[1:])
            out["pressure"] = val / 10.0 + (1000.0 if val < 5000 else 0.0)
        elif tok[0] == "4" and len(tok) == 5 and tok[1:].isdigit():
            val = int(tok[1:])
            out["pressure_sl"] = val / 10.0 + (1000.0 if val < 5000 else 0.0)
        elif tok[0] == "6" and len(tok) == 5 and tok[1:].isdigit() and sec3:
            v = int(tok[1:4])
            # WMO табл. 4019: 001-988 = целые мм; 991-999 = следы 0.1-0.9 мм; 990 = нет
            out["precip_mm"] = ((v - 990) / 10.0 if v >= 991 else
                                None if v == 990 else float(v))
    return out


def collect_ogimet_synop(hours_back=30):
    """SYNOP станции WMO 27459 через OGIMET -> observations.db (raw + разбор)."""
    import urllib.parse as up
    now = datetime.now(timezone.utc)
    begin = (now - timedelta(hours=hours_back)).strftime("%Y%m%d%H%M")
    end = (now + timedelta(minutes=10)).strftime("%Y%m%d%H%M")
    url = ("https://www.ogimet.com/cgi-bin/getsynop?block=27459"
           f"&begin={begin}&end={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "weather-validation/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    conn = _history_conn()
    n = 0
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS synop_raw (ts_utc TEXT UNIQUE, raw TEXT)")
        try:
            conn.execute("ALTER TABLE observations ADD COLUMN phys_station TEXT")
        except Exception:
            pass
        for line in text.splitlines():
            line = line.strip().lstrip("#")
            if not line or "," not in line:
                continue
            parts = line.split(",", 6)
            if len(parts) < 7:
                continue
            y, mo, d, h, mi = parts[1:6]
            raw_msg = parts[6].strip()
            try:
                ts_utc = datetime(int(y), int(mo), int(d), int(h), int(mi))
            except ValueError:
                continue
            ts_utc_iso = ts_utc.isoformat(timespec="seconds") + "Z"
            try:
                from zoneinfo import ZoneInfo
                from datetime import timezone as _dt_utc
                # ts_utc парсится naive: сначала помечаем UTC, потом МСК, потом снова naive
                # (astimezone на naive трактует его как system-local — был баг: строки оставались UTC)
                ts_local = (ts_utc.replace(tzinfo=_dt_utc)
                             .astimezone(ZoneInfo("Europe/Moscow"))
                             .replace(tzinfo=None).isoformat(timespec="seconds"))
            except Exception:
                ts_local = (ts_utc + timedelta(hours=3)).isoformat(timespec="seconds")
            decoded = _decode_synop(raw_msg)
            conn.execute("INSERT OR REPLACE INTO synop_raw VALUES (?,?)",
                         (ts_utc_iso, raw_msg))
            conn.execute(
                "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?)",
                (ts_local, "Nizhny Novgorod WMO27459", "ogimet-synop",
                 decoded["temperature"], None, decoded["pressure"],
                 decoded["precip_mm"], "27459"))
            n += 1
        conn.commit()
    finally:
        conn.close()
    log.info("observations: OGIMET SYNOP 27459 — %d сообщений", n)
    return n


def accuracy_query(provider, lead, days, phys="", window=1.0):
    """MAE температуры прогнозов против наблюдений (тестируемая функция /accuracy)."""
    conn = _history_conn()
    try:
        cur = conn.execute("""
                        SELECT avg(abs(fp.temperature - o.temperature)), count(*)
                        FROM forecast_points fp
                        JOIN observations o
                          ON abs(julianday(o.ts) - julianday(fp.valid_at)) <= ? / 24.0
                         AND o.temperature IS NOT NULL
                        WHERE lower(fp.provider) = lower(?) AND fp.temperature IS NOT NULL
                          AND (? = '' OR o.phys_station = ?)
                          AND (julianday(fp.valid_at) - julianday(fp.created)) * 24 BETWEEN ? AND ?
                          AND fp.created > datetime('now', ?)""",
                        (window, provider, phys, phys,
                         max(0, lead - 1), lead + 1, f"-{days} days"))
        mae, n = cur.fetchone()
        return (round(mae, 2) if mae is not None else None), (n or 0)
    finally:
        conn.close()


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
    rotate_stats_if_needed()
    prune_history()
    collect_foreca_observations()
    while True:
        interval = 3600
        try:
            cfg = load_config()
            if cfg.get("stats_enabled", True):
                stats_collect_non_yandex(cfg)
                interval = max(300, int(cfg.get("stats_interval_minutes", 60)) * 60)
        except Exception as e:
            log.error("stats loop error: %s", e)
        rotate_stats_if_needed()
        prune_history()
        collect_foreca_observations()  # почасовой ground truth для /accuracy
        try:
            collect_ogimet_synop(hours_back=30)
        except Exception as e:
            log.warning("observations: ogimet tick failed: %s", e)
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
    temp = round(main["temp"]) if main.get("temp") is not None else None
    wind_speed = (round(wind["speed"], 1) if wind.get("speed") is not None else None)
    wind_angle = int(wind["deg"]) if wind.get("deg") is not None else None
    humidity = int(main["humidity"]) if main.get("humidity") is not None else None
    pressure_mm = (round(int(main["pressure"]) * 0.750062)
                   if main.get("pressure") is not None else None)
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
            "temp": (round(it["main"]["temp"]) if it.get("main", {}).get("temp") is not None else None),
            "humidity": (int(it["main"]["humidity"]) if it.get("main", {}).get("humidity") is not None else None),
            "wind_speed": (round(it["wind"]["speed"], 1) if it.get("wind", {}).get("speed") is not None else None),
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
                "moon_code": moon_phase_code(),
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

    temp = round(cw["temperature"]) if cw.get("temperature") is not None else None
    wind_speed = (round(cw["windspeed"] / 3.6, 1) if cw.get("windspeed") is not None else None)
    wind_angle = cw.get("winddirection")

    curr_time = cw.get("time", "")
    h_idx = None
    times = hourly.get("time") or []
    # последний ПРОШЕДШИЙ час: cw.time с минутами ("00:30") не совпадёт точно
    if curr_time and times:
        best = None
        for i, tstr in enumerate(times):
            if str(tstr) <= curr_time:
                best = i
            else:
                break
        h_idx = best

    rh_list = hourly.get("relativehumidity_2m") or []
    sp_list = hourly.get("surface_pressure") or []
    humidity = (rh_list[h_idx] if h_idx is not None and h_idx < len(rh_list) and rh_list[h_idx] is not None else None)
    pressure_mm = (round(sp_list[h_idx] * 0.750062)
                   if h_idx is not None and h_idx < len(sp_list) and sp_list[h_idx] is not None else None)

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

    sunrise = daily["sunrise"][0].split("T")[-1][:5] if daily.get("sunrise") else None
    sunset = daily["sunset"][0].split("T")[-1][:5] if daily.get("sunset") else None

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
                "moon_code": moon_phase_code(),
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

    temp = int(cc["temp_C"]) if cc.get("temp_C") is not None else None
    humidity = int(cc["humidity"]) if cc.get("humidity") is not None else None
    pressure_mm = (round(int(cc["pressure"]) * 0.750062)
                   if cc.get("pressure") is not None else None)
    wind_speed = (round(int(cc["windspeedKmph"]) / 3.6, 1)
                  if cc.get("windspeedKmph") is not None else None)
    wind_angle = int(cc["winddirDegree"]) if cc.get("winddirDegree") is not None else None

    hour_now = datetime.now().hour
    suf = "_d" if 8 <= hour_now < 20 else "_n"
    icon = _icon_with_suffix(_wwo_icon(cc.get("weatherCode", "116")), 8 <= hour_now < 20)

    astro = (today.get("astronomy") or [{}])[0]
    sunrise = _fmt12to24(astro["sunrise"]) if astro.get("sunrise") else None
    sunset = _fmt12to24(astro["sunset"]) if astro.get("sunset") else None

    hours_list = []
    for h in range(24):
        b = hourly[min(7, h // 3)] if hourly else {}
        _tv = b.get("tempC", temp)
        hours_list.append({"hour": str(h), "temp": (int(_tv) if _tv is not None else None)})

    def block_part(idx, fallback_icon):
        b = hourly[idx] if len(hourly) > idx else {}
        hh = int(b.get("time", "0") or 0) // 100
        s2 = "_d" if 8 <= hh < 20 else "_n"
        return {
            "temp_avg": (int(_tv2) if (_tv2 := b.get("tempC", temp)) is not None else None),
            "icon": _icon_with_suffix(_wwo_icon(b.get("weatherCode", "116")), 8 <= hh < 20),
            "wind_speed": (round(int(b["windspeedKmph"]) / 3.6, 1) if b.get("windspeedKmph") is not None else None),
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
                "moon_code": moon_phase_code(),
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
           f"&ac=0&unit=metric&output=json&tzshift=3")
    st = json.loads(fetch_url_via(url, None, 25))
    ds = st.get("dataseries") or []
    init = st.get("init", "")
    # init 7timer — UTC (параметр tzshift API игнорирует): приводим к локальному МСК
    init_hour = (int(init[8:10]) + 3) % 24 if len(init) >= 10 else 12

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
            return {"temp_avg": None, "icon": fallback_icon, "wind_speed": None,
                    "wind_angle": None}
        w10 = b.get("wind10m", {}) or {}
        bft = w10.get("speed")
        direction = (w10.get("direction") or "")[:3]
        return {
            "temp_avg": (int(b["temp2m"]) if b.get("temp2m") is not None else None),
            "icon": _icon_with_suffix(b_icon(b), 6 <= hour < 18),
            "wind_speed": (_7TIMER_BFT_MS[min(12, int(bft))] if bft is not None else None),
            "wind_angle": _7TIMER_DIR_DEG.get(direction)
        }

    b0 = block_at(datetime.now().hour)
    temp = int(b0["temp2m"]) if b0.get("temp2m") is not None else None
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
                "moon_code": moon_phase_code(),
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
                    record_forecast("Yandex", key, data)
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
            record_forecast("OpenWeatherMap", key, owm_data)
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
            record_forecast("Open-Meteo", key, om_data)
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
            record_forecast("Gismeteo", key, g_data)
            log.info("Successfully updated weather via Gismeteo fallback (temp: %s°)", g_data["fact"].get("temp"))
            return g_data
        except Exception as e:
            last_error_message = f"Gismeteo: {e}"
            log.error("Gismeteo fallback failed: %s", e)

    # Fallback: Foreca (Bearer-токен в config: foreca_api_key)
    if want("foreca") and load_config().get("enable_foreca_fallback", True) and load_config().get("foreca_api_key", ""):
        log.info("Attempting fallback to Foreca...")
        try:
            fc_data = FORECA.get_weather(latitude=lat, longitude=lon)
            _store_location_result(key, fc_data, "Active fallback: Foreca")
            stats_log_source("Foreca", fc_data)
            record_forecast("Foreca", key, fc_data)
            log.info("Successfully updated weather via Foreca fallback (temp: %s°)", fc_data["fact"].get("temp"))
            return fc_data
        except Exception as e:
            last_error_message = f"Foreca: {e}"
            log.error("Foreca fallback failed: %s", e)

    # Fallback 2: wttr.in (без ключа)
    if want("wttr") and load_config().get("enable_wttr_fallback", True):
        log.info("Attempting fallback to wttr.in...")
        try:
            wt_data = fetch_from_wttr(lat, lon)
            wt_data["src"] = "wttr.in"
            _store_location_result(key, wt_data, "Active fallback: wttr.in (Yandex/OM unavailable)")
            stats_log_source("wttr.in", wt_data)
            record_forecast("wttr.in", key, wt_data)
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
            record_forecast("7timer", key, st_data)
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
    # Заморозка однопоточного сервера на полумёртвом клиентском сокете
    # (keep-alive readline без таймаута) — закрываем соединение через 30 с
    timeout = 30

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
                self.wfile.write(json.dumps({"error": "geocode failed"}).encode("utf-8"))
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
                self.wfile.write(json.dumps({"error": "reverse failed"}).encode("utf-8"))
            return

        if path == "/observations/fetch":
            global last_obs_fetch
            if time.time() - last_obs_fetch < 600:
                self.send_response(429)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "too soon"}).encode("utf-8"))
                return
            last_obs_fetch = time.time()
            n = collect_foreca_observations()
            try:
                n += collect_ogimet_synop(hours_back=30)
            except Exception as e:
                log.warning("observations: ogimet failed: %s", e)
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"fetched": n}).encode("utf-8"))
            return

        if path == "/accuracy":
            qs = parse_qs(urlparse(self.path).query)
            provider = (qs.get("provider", [""])[0] or "").strip()
            phys = (qs.get("phys", [""])[0] or "").strip()
            try:
                window = float(qs.get("window", ["1.0"])[0])
            except ValueError:
                window = 1.0
            try:
                lead = int(qs.get("lead_hours", ["24"])[0])
                days = int(qs.get("days", ["90"])[0])
            except ValueError:
                lead, days = 24, 90
            if not provider or lead < 0:
                self.send_response(400)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "provider and lead_hours required"}).encode("utf-8"))
                return
            try:
                mae, n = accuracy_query(provider, lead, days, phys, window)
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "provider": provider, "lead_hours": lead,
                    "samples": n or 0,
                    "mae_temp_c": (round(mae, 2) if mae is not None else None),
                }, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log.warning("accuracy failed: %s", e)
                self.send_response(500)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "internal error"}).encode("utf-8"))
            return

        # Погодные маршруты — только известные: остальное 404,
        # чтобы сканеры/favicon не запускали фетчи и не жгли квоту
        if path not in ("/", "/weather.json", "/forecast.json", "/v2/forecast"):
            self.send_response(404)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))
            return

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
        valid_sources = ("yandex", "owm", "om", "gismeteo", "foreca", "wttr", "7timer")
        sources = None
        if src_filter:
            if src_filter not in valid_sources:
                self.send_response(400)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "unknown source",
                    "valid": list(valid_sources)}).encode("utf-8"))
                return
            sources = (src_filter,)
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
    server = ThreadingHTTPServer(("0.0.0.0", port), WeatherHTTPHandler)
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
