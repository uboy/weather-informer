#!/usr/bin/env python3
"""
GismeteoProvider — погодный провайдер Gismeteo без API-токена.

Использует внутренний сервис Gismeteo (inform-service "inf_chrome"):
    /cities/?lat={lat}&lng={lng}&count=1&lang=ru  -> ближайший город (XML)
    /forecast/?city={city_id}&lang=ru             -> прогноз 3-часовыми точками (XML)

Это НЕ официальный публичный API (api.gismeteo.net/v4 с X-Gismeteo-Token),
а внутренний endpoint: формат/XML может измениться в любой момент, поэтому вся
работа с ним (запросы, парсинг, конвертация) изолирована в этом классе и не
должна протекать в caching_server.py и UI.

Точки прогноза идут с шагом ~3 часа. Никакой интерполяции не выполняется:
исходные точки сохраняются в результате (gismeteo_points, interpolated=false),
почасовые hours берутся выдержкой ближайшей точки без придумывания значений.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger("WeatherCache")


class GismeteoError(Exception):
    """Ошибки провайдера Gismeteo (сеть, HTTP, XML, город не найден)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GismeteoProvider:
    # Резервные хосты: при неудаче первого — автоматический переход на второй
    HOSTS = [
        "https://services.gismeteo.ru/inform-service/inf_chrome",
        "https://services.gismeteo.net/inform-service/inf_chrome",
    ]
    USER_AGENT = "WeatherInformerLocal/1.0 (lan weather kiosk; gismeteo inform-service)"
    TIMEOUT = 10
    RETRIES = 2          # повторы только для временных сетевых ошибок, на хост
    FORECAST_TTL = 20 * 60     # кэш прогноза: 20 минут
    CITY_TTL = 30 * 24 * 3600  # кэш city_id: 30 дней
    FORECAST_HOURS = 48        # горизонт прогноза

    # Румбы Gismeteo (wd) -> градусы
    WD_DEG = [0, 45, 90, 135, 180, 225, 270, 315]

    def __init__(self, geocode_fn=None, city_cache_path=None, logger=None):
        """
        geocode_fn: callable(name:str) -> list[{name, lat, lon}], поиск города
                    по названию (в проекте — Nominatim-геокодер сервера).
        city_cache_path: путь json-файла долговременного кэша координаты -> city_id.
        """
        self._geocode_fn = geocode_fn
        self._city_cache_path = city_cache_path
        self.log = logger or log
        self._city_cache = {}    # "lat:lon" -> {"id", "name", "tzone", "ts"}
        self._fc_cache = {}      # city_id -> {"data": dict, "ts": float}
        self._load_city_cache()

    # ------------------------------------------------------------------ #
    # Публичный интерфейс
    # ------------------------------------------------------------------ #
    def get_weather(self, city: str | None = None,
                    latitude: float | None = None,
                    longitude: float | None = None) -> dict:
        """
        Прогноз в формате информера (Яндекс-подобном).
        Приоритет: координаты, затем название города.
        """
        if latitude is None and longitude is None and not city:
            raise GismeteoError("Не передан ни город, ни координаты")
        if (latitude is None) != (longitude is None):
            raise GismeteoError("Передана только одна из координат (нужны lat и lon вместе)")
        if latitude is not None:
            latitude, longitude = self._validate_coords(latitude, longitude)
            place = self._resolve_city_by_coords(latitude, longitude)
        else:
            place = self._resolve_city_by_name(str(city))

        return self._get_forecast_for_place(place)

    # ------------------------------------------------------------------ #
    # Разрешение города
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_coords(lat, lon):
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise GismeteoError(f"Некорректные координаты: {lat!r}, {lon!r}")
        if not (-90.0 <= lat <= 90.0):
            raise GismeteoError(f"Широта вне диапазона: {lat}")
        if not (-180.0 <= lon <= 180.0):
            raise GismeteoError(f"Долгота вне диапазона: {lon}")
        return round(lat, 4), round(lon, 4)

    def _resolve_city_by_coords(self, lat, lon) -> dict:
        key = f"{round(lat, 2)}:{round(lon, 2)}"
        hit = self._city_cache.get(key)
        if hit and (time.time() - hit["ts"] < self.CITY_TTL):
            return hit
        xml = self._get("/cities/", {"lat": lat, "lng": lon, "count": 1, "lang": "ru"})
        items = self._parse_cities(xml)
        if not items:
            raise GismeteoError(f"Gismeteo: город по координатам ({lat}, {lon}) не найден")
        place = items[0]
        place["lat"], place["lon"] = lat, lon
        place["ts"] = time.time()
        self._city_cache[key] = place
        self._save_city_cache()
        self.log.info("Gismeteo: координаты (%s, %s) -> city %s «%s»",
                      lat, lon, place["id"], place["name"])
        return place

    def _resolve_city_by_name(self, name: str) -> dict:
        name = name.strip()
        if len(name) < 2:
            raise GismeteoError("Название города слишком короткое")
        if not self._geocode_fn:
            raise GismeteoError("Геокодер по названию недоступен")
        results = self._geocode_fn(name)
        if not results:
            raise GismeteoError(f"Геокодер не нашёл город «{name}»")
        geo = results[0]
        self.log.info("Gismeteo: город «%s» -> %s (%s, %s)", name, geo["name"], geo["lat"], geo["lon"])
        return self._resolve_city_by_coords(float(geo["lat"]), float(geo["lon"]))

    # ------------------------------------------------------------------ #
    # Прогноз
    # ------------------------------------------------------------------ #
    def _get_forecast_for_place(self, place: dict) -> dict:
        city_id = place["id"]
        hit = self._fc_cache.get(city_id)
        if hit and (time.time() - hit["ts"] < self.FORECAST_TTL):
            return hit["data"]

        xml = self._get("/forecast/", {"city": city_id, "lang": "ru"})
        parsed = self._parse_forecast(xml)
        data = self._convert(parsed, place)
        self._fc_cache[city_id] = {"data": data, "ts": time.time()}
        return data

    # ------------------------------------------------------------------ #
    # HTTP: фолбэк хостов, retry на временных ошибках, таймауты
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict) -> bytes:
        qs = urllib.parse.urlencode(params)
        last_exc = None
        for host in self.HOSTS:
            url = f"{host}{path}?{qs}"
            for attempt in range(1, self.RETRIES + 1):
                try:
                    return self._http_get(url, self.TIMEOUT)
                except GismeteoError as e:
                    if e.status == 403:
                        # блокировка конкретного домена — пробуем второй хост
                        self.log.warning("Gismeteo: %s вернул 403, пробую другой хост", host)
                        last_exc = e
                        break
                    raise  # 429/некорректный ответ — другой хост тот же бэкенд, повтор бессмыслен
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_exc = e
                    self.log.warning("Gismeteo: попытка %d/%d %s не удалась: %s",
                                     attempt, self.RETRIES, host, e)
                    if attempt < self.RETRIES:
                        time.sleep(1)
        raise GismeteoError(f"Gismeteo недоступен (все хосты): {last_exc}")

    @staticmethod
    def _http_get(url: str, timeout: float) -> bytes:
        """Изолированный HTTP GET; переопределяется в тестах."""
        req = urllib.request.Request(url, headers={"User-Agent": GismeteoProvider.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status >= 500:
                    raise OSError(f"HTTP {resp.status}")
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise GismeteoError(f"Gismeteo HTTP {e.code}: доступ ограничен", status=e.code)
            if e.code >= 500:
                raise OSError(f"Gismeteo HTTP {e.code}")
            raise GismeteoError(f"Gismeteo HTTP {e.code}")

    # ------------------------------------------------------------------ #
    # Парсинг XML
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_cities(xml_bytes: bytes) -> list:
        """<document><item id lat lng tzone n kind distance .../> -> список городов."""
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            raise GismeteoError(f"Gismeteo: некорректный XML cities: {e}")
        items = []
        for it in root.findall("item"):
            try:
                items.append({
                    "id": str(it.get("id")),
                    "name": (it.get("n") or "").strip(),
                    "lat": float(it.get("lat")),
                    "lon": float(it.get("lng")),
                    "tzone_min": int(it.get("tzone", "0")),
                    "kind": it.get("kind", ""),
                    "distance": float(it.get("distance", "1e9")),
                    "district": it.get("district_name", ""),
                })
            except (TypeError, ValueError):
                continue
        # приоритет настоящему городу среди ближайших (kind=T), иначе просто ближайший
        cities = [i for i in items if i["kind"] == "T"]
        return (cities or items)[:1]

    @staticmethod
    def _parse_forecast(xml_bytes: bytes) -> dict:
        """<weather><location><fact/><day><forecast><values .../></day></location>"""
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            raise GismeteoError(f"Gismeteo: некорректный XML forecast: {e}")
        loc = root.find("location")
        if loc is None:
            raise GismeteoError("Gismeteo: в XML нет <location>")
        try:
            tzone_min = int(loc.get("tzone", "0"))
        except ValueError:
            tzone_min = 0

        fact_el = loc.find("fact")
        fact = None
        if fact_el is not None:
            vals = fact_el.find("values")
            if vals is not None:
                fact = dict(vals.attrib)
                fact.update(fact_el.attrib)  # valid, tod, risem, setm, sunrise, sunset


        points = []
        for day in loc.findall("day"):
            for fc in day.findall("forecast"):
                vals = fc.find("values")
                if vals is None:
                    continue
                p = dict(vals.attrib)
                p["valid"] = fc.get("valid", "")
                points.append(p)
        if not points:
            raise GismeteoError("Gismeteo: прогноз не содержит точек")

        return {
            "name": loc.get("name", ""),
            "tzone_min": tzone_min,
            "fact": fact,
            "points": points,
        }

    # ------------------------------------------------------------------ #
    # Конвертация в формат информера (Яндекс-подобный)
    # ------------------------------------------------------------------ #
    def _convert(self, parsed: dict, place: dict) -> dict:
        tz = timedelta(minutes=parsed["tzone_min"])
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # Горизонт: от начала текущих ЛОКАЛЬНЫХ суток города до now+48ч.
        # Прошедшие часы сегодняшнего дня нужны для parts (night/morning/day)
        local_now = now_utc + tz
        today_start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - tz
        horizon = now_utc + timedelta(hours=self.FORECAST_HOURS)
        raw_points = []
        for p in parsed["points"]:
            try:
                dt_utc = parse_valid_dt(p["valid"])
            except (KeyError, ValueError):
                continue
            if dt_utc < today_start_utc or dt_utc > horizon:
                continue  # чужие сутки и горизонт >48ч — отсекаем
            raw_points.append({
                "_dt_utc": dt_utc,
                "_dt_local": dt_utc + tz,
                "valid_utc": dt_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "valid_local": (dt_utc + tz).strftime("%Y-%m-%dT%H:%M:%S"),
                "temperature": self._f(p.get("t")),
                "pressure_mm": self._f(p.get("p")),
                "humidity": self._i(p.get("hum")),
                "wind_speed": self._f(p.get("ws")),
                "wind_direction": self.WD_DEG[self._i(p.get("wd"), 0) % 8] if p.get("wd") is not None else None,
                "precipitation_mm": self._f(p.get("pr")),
                "precipitation_type": self._i(p.get("pt"), 0),
                "cloudiness": self._i(p.get("cl"), -1),
                "thunder": self._i(p.get("ts"), 0),
                "condition": p.get("descr", ""),
                "source": "gismeteo",
                "interpolated": False,
            })
        if not raw_points:
            raise GismeteoError("Gismeteo: после фильтра 48ч не осталось точек")

        # fact: штатный <fact> XML, иначе первая точка
        fvals = parsed["fact"] or {}
        cur = raw_points[0]
        try:
            fact_hour = (parse_valid_dt(fvals["valid"]) + tz).hour
        except (KeyError, ValueError):
            fact_hour = cur["_dt_local"].hour
        fact = {
            "temp": self._i(fvals.get("t"), cur["temperature"] if cur["temperature"] is not None else 0),
            "icon": self._icon(fvals, fact_hour),
            "wind_speed": self._f(fvals.get("ws"), cur["wind_speed"] or 0),
            "wind_angle": self.WD_DEG[self._i(fvals.get("wd"), 0) % 8] if fvals.get("wd") is not None else (cur["wind_direction"] or 180),
            "humidity": self._i(fvals.get("hum"), cur["humidity"] or 50),
            "pressure_mm": self._i(fvals.get("p"), cur["pressure_mm"] or 748),
            "condition": fvals.get("descr", cur["condition"]),
        }

        # parts: группировка точек по ЛОКАЛЬНОМУ времени города
        parts, next_night = self._build_parts(raw_points, tz, now_utc)
        hours = self._build_hours(raw_points, tz, now_utc)

        rise, sett = self._sun_times(parsed)
        forecasts = [{
            "sunrise": rise,
            "sunset": sett,
            "moon_code": 9,
            "hours": hours,
            "parts": parts,
        }, {"parts": next_night}]

        return {
            "src": "Gismeteo",
            "fact": fact,
            "forecasts": forecasts,
            "gismeteo_points": [self._public_point(p) for p in raw_points],
        }

    # ------------------------------------------------------------------ #
    # Вспомогательные
    # ------------------------------------------------------------------ #
    @staticmethod
    def _f(v, default=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _i(v, default=None):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    def _icon(self, vals: dict, local_hour: int) -> str:
        """Gismeteo values -> класс значка информера (skc/bkn/ovc + _d/_n)."""
        cl = self._i(vals.get("cl"), None) if vals else None
        pt = self._i(vals.get("pt"), 0) if vals else 0
        pr = self._f(vals.get("pr"), 0) if vals else 0
        ts = self._i(vals.get("ts"), 0) if vals else 0
        t = self._f(vals.get("t"), None) if vals else None
        suffix = "_d" if 6 <= local_hour < 20 else "_n"

        if ts and ts > 0:
            return "ovc_ts"
        if pt in (1, 3) or (pr and pr > 0 and (t is None or t > 0)):
            return "ovc_ra"
        if pt == 2 or (pr and pr > 0 and t is not None and t <= 0):
            return "ovc_sn"
        if cl == 0:
            return "skc" + suffix
        if cl in (1, 2):
            return "bkn" + suffix
        if cl == 3:
            return "ovc"
        # cl неизвестен — по текстовому описанию
        descr = (vals.get("descr", "") if vals else "").lower()
        if "гроз" in descr:
            return "ovc_ts"
        if "дожд" in descr:
            return "ovc_ra"
        if "снег" in descr:
            return "ovc_sn"
        if "пасмурн" in descr or ("облачн" in descr and "мало" not in descr):
            return "ovc"
        if "малообл" in descr or "перемен" in descr:
            return "bkn" + suffix
        if "ясно" in descr or "солнечн" in descr:
            return "skc" + suffix
        return "bkn" + suffix

    def _build_parts(self, points, tz, now_utc):
        """4 части текущих суток + ночь следующих, по локальному времени города."""
        today = (now_utc + tz).date()
        tomorrow = today + timedelta(days=1)

        def sel(date, h0, h1):
            sel_pts = [p for p in points
                       if p["_dt_local"].date() == date and h0 <= p["_dt_local"].hour < h1]
            return sel_pts

        def make(date, h0, h1):
            pts = sel(date, h0, h1)
            if not pts:
                return None
            temps = [p["temperature"] for p in pts if p["temperature"] is not None]
            winds = [p["wind_speed"] for p in pts if p["wind_speed"] is not None]
            mid = pts[len(pts) // 2]
            icon = self._icon_from_point(mid)
            return {
                "temp_avg": round(sum(temps) / len(temps)) if temps else 0,
                "icon": icon,
                "wind_speed": round(sum(winds) / len(winds), 1) if winds else 0,
                "wind_angle": mid["wind_direction"] or 180,
            }

        def part_or(date, h0, h1, fallback):
            p = make(date, h0, h1)
            return p if p else {"temp_avg": 0, "icon": fallback, "wind_speed": 0, "wind_angle": 180}

        bkn_d, bkn_n = "bkn_d", "bkn_n"
        parts = {
            "night": part_or(today, 0, 6, bkn_n),
            "morning": part_or(today, 6, 12, bkn_d),
            "day": part_or(today, 12, 18, bkn_d),
            "evening": part_or(today, 18, 24, bkn_n),
        }
        next_night = {
            "night": part_or(tomorrow, 0, 6, bkn_n),
            "morning": part_or(tomorrow, 6, 12, bkn_d),
        }
        return parts, next_night

    def _icon_from_point(self, p):
        """Значок из уже разобранной точки (по условию/типу осадков/облачности)."""
        if p["thunder"]:
            return "ovc_ts"
        if p["precipitation_type"] in (1, 3) or (p["precipitation_mm"] and p["precipitation_mm"] > 0 and (p["temperature"] is None or p["temperature"] > 0)):
            return "ovc_ra"
        if p["precipitation_type"] == 2 or (p["precipitation_mm"] and p["precipitation_mm"] > 0 and p["temperature"] is not None and p["temperature"] <= 0):
            return "ovc_sn"
        cl = p["cloudiness"]
        suffix = "_d" if 6 <= p["_dt_local"].hour < 20 else "_n"
        if cl == 0:
            return "skc" + suffix
        if cl in (1, 2):
            return "bkn" + suffix
        if cl == 3:
            return "ovc"
        return "bkn" + suffix

    def _build_hours(self, points, tz, now_utc):
        """24 часа текущих локальных суток: выдержка ближайшей точки (без интерполяции)."""
        today = (now_utc + tz).date()
        by_hour = {}
        for p in points:
            if p["_dt_local"].date() != today:
                continue
            h = p["_dt_local"].hour
            if h not in by_hour:
                by_hour[h] = p
        out = []
        for h in range(24):
            # ближайшая доступная точка этого дня
            pt = None
            for dh in (0, 1, 2, 3, -1, -2, -3, 4, -4):
                cand = (h + dh) % 24
                if cand in by_hour:
                    pt = by_hour[cand]
                    break
            t = pt["temperature"] if pt else None
            out.append({"hour": str(h), "temp": int(t) if t is not None else 0})
        return out

    @staticmethod
    def _sun_times(parsed):
        """Восход/закат «ЧЧ:ММ» из fact-атрибутов risem/setm (минуты от полуночи)."""
        def fmt(mins):
            if mins is None:
                return "06:00"
            mins = int(mins) % (24 * 60)
            return f"{mins // 60:02d}:{mins % 60:02d}"
        fact = parsed.get("fact") or {}
        return fmt(fact.get("risem")), fmt(fact.get("setm"))

    @staticmethod
    def _public_point(p):
        out = {k: v for k, v in p.items() if not k.startswith("_")}
        return out

    # ------------------------------------------------------------------ #
    # Долговременный кэш city_id
    # ------------------------------------------------------------------ #
    def _load_city_cache(self):
        if not self._city_cache_path or not os.path.isfile(self._city_cache_path):
            return
        try:
            with open(self._city_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            self._city_cache = {k: v for k, v in data.items()
                                if now - v.get("ts", 0) < self.CITY_TTL}
        except (OSError, ValueError) as e:
            self.log.warning("Gismeteo: не удалось прочитать кэш городов: %s", e)

    def _save_city_cache(self):
        if not self._city_cache_path:
            return
        try:
            tmp = self._city_cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._city_cache, f, ensure_ascii=False)
            os.replace(tmp, self._city_cache_path)
        except OSError as e:
            self.log.warning("Gismeteo: не удалось сохранить кэш городов: %s", e)


def parse_valid_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
