#!/usr/bin/env python3
"""
ForecaProvider — погодный провайдер Foreca (weatherapi.foreca.net, Bearer-токен).

Не официальный выбор владельца парка: эндпоинты
    /location/search/{query}          -> локейшены (id, name, timezone)
    /forecast/hourly/{id}             -> почасовой прогноз (periods до 169)
    /observation/latest/{id}          -> наблюдения ближайших станций

Вся работа с API изолирована в этом классе (изменение эндпоинтов/формата
чинится только здесь). Прогноз Foreca НЕ содержит humidity/pressure —
в конвертации они честно уходят в null.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
from datetime import datetime

log = logging.getLogger("WeatherCache")


class ForecaError(Exception):
    """Ошибки провайдера Foreca (сеть, HTTP, JSON, локейшен не найден)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ForecaProvider:
    API = "https://weatherapi.foreca.net/api/v1"
    USER_AGENT = "WeatherInformerLocal/1.0 (lan weather kiosk; foreca provider)"
    TIMEOUT = 10
    RETRIES = 2
    FORECAST_TTL = 20 * 60     # кэш прогноза: 20 минут
    LOCATION_TTL = 30 * 24 * 3600
    FORECAST_HOURS = 48

    def __init__(self, token, geocode_fn=None, reverse_fn=None,
                 location_cache_path=None, logger=None):
        self._token = (token or "").strip()
        self._geocode_fn = geocode_fn        # name -> [{name, lat, lon}]
        self._reverse_fn = reverse_fn        # (lat, lon) -> {name}
        self._location_cache_path = location_cache_path
        self.log = logger or log
        self._loc_cache = {}   # "lat:lon"/name -> {"id", "name", "tz", "ts"}
        self._fc_cache = {}    # loc_id -> {"data": dict, "ts": float}
        self._load_location_cache()

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = (value or "").strip()

    # ------------------------------------------------------------------ #
    def get_weather(self, city=None, latitude=None, longitude=None) -> dict:
        """Прогноз в формате информера (Яндекс-подобном)."""
        place = self._resolve_location(city, latitude, longitude)
        return self._get_forecast_for_place(place)

    def get_observations(self, city=None, latitude=None, longitude=None,
                         stations=6) -> list:
        """Наблюдения ближайших станций (ground truth)."""
        place = self._resolve_location(city, latitude, longitude)
        return self._fetch_observations(place["id"], stations)

    # ------------------------------------------------------------------ #
    # Разрешение локейшена
    # ------------------------------------------------------------------ #
    def _resolve_location(self, city, latitude, longitude) -> dict:
        if latitude is not None and self._reverse_fn:
            rev = self._reverse_fn(latitude, longitude)
            name = (rev or {}).get("name", "")
            if name:
                return self._search_location(name, latitude, longitude)
        if city:
            return self._search_location(str(city).strip(), latitude, longitude)
        raise ForecaError("Foreca: не передан ни город, ни координаты с геокодером")

    def _search_location(self, query, latitude=None, longitude=None) -> dict:
        key = f"{round(latitude or 0, 2)}:{round(longitude or 0, 2)}" if latitude is not None else query.lower()
        hit = self._loc_cache.get(key)
        if hit and (time.time() - hit.get("ts", 0) < self.LOCATION_TTL):
            return hit
        params = {"lang": "ru"}
        data = self._get("/location/search/" + urllib.parse.quote(query), params)
        locations = data.get("locations") or []
        if not locations:
            raise ForecaError(f"Foreca: локейшен по запросу «{query}» не найден")
        it = locations[0]
        place = {
            "id": it["id"],
            "name": it.get("name", query),
            "tz": it.get("timezone", "Europe/Moscow"),
            "ts": time.time(),
        }
        self._loc_cache[key] = place
        self._save_location_cache()
        self.log.info("Foreca: «%s» -> id %s (%s, %s)", query, place["id"], place["name"], place["tz"])
        return place

    # ------------------------------------------------------------------ #
    # Прогноз
    # ------------------------------------------------------------------ #
    def _get_forecast_for_place(self, place: dict) -> dict:
        loc_id = place["id"]
        hit = self._fc_cache.get(str(loc_id))
        if hit and (time.time() - hit["ts"] < self.FORECAST_TTL):
            return hit["data"]
        data = self._get(f"/forecast/hourly/{loc_id}", {
            "periods": 49, "tz": place.get("tz", "Europe/Moscow"),
            "tempunit": "C", "windunit": "MS", "rounding": 0})
        periods = data.get("forecast") or []
        if not periods:
            raise ForecaError("Foreca: пустой прогноз")
        converted = self._convert(periods, place)
        self._fc_cache[str(loc_id)] = {"data": converted, "ts": time.time()}
        return converted

    # ------------------------------------------------------------------ #
    # Наблюдения (ground truth)
    # ------------------------------------------------------------------ #
    def _fetch_observations(self, loc_id, stations=6) -> list:
        data = self._get(f"/observation/latest/{loc_id}", {
            "stations": stations, "prec": 2,
            "tz": "Europe/Moscow", "rounding": 0})
        out = []
        for obs in (data.get("observations") or []):
            precip1h = obs.get("precip1h") or []
            precip_mm = None
            if precip1h:
                acc = [p.get("accum") for p in precip1h if p.get("accum") is not None]
                precip_mm = round(sum(acc), 2) if acc else 0.0
            out.append({
                "station": obs.get("station", ""),
                "time": obs.get("time", ""),
                "temperature": obs.get("temperature"),
                "relHumidity": obs.get("relHumidity"),
                "pressure": obs.get("pressure"),
                "precip_mm": precip_mm,
                "distance": obs.get("distance", ""),
            })
        return out

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _mask_token(self, text) -> str:
        s = str(text)
        if self._token and len(self._token) > 4:
            s = s.replace(self._token, "******")
        return s

    def _get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        url = f"{self.API}{path}?{qs}"
        last_exc = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                return json.loads(self._http_get(url, self.TIMEOUT))
            except ForecaError as e:
                if e.status in (403, 429):
                    raise  # токен/лимит — повтор бессмысленен
                last_exc = e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last_exc = self._mask_token(e)
                self.log.warning("Foreca: попытка %d/%d не удалась: %s", attempt, self.RETRIES, last_exc)
                if attempt < self.RETRIES:
                    time.sleep(1)
        raise ForecaError(f"Foreca недоступен: {self._mask_token(last_exc)}")

    def _http_get(self, url: str, timeout: float) -> bytes:
        """Изолированный GET с Bearer-токеном; переопределяется в тестах."""
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self.USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status >= 500:
                    raise OSError(f"HTTP {resp.status}")
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ForecaError(f"Foreca HTTP {e.code}: токен недействителен", status=e.code)
            if e.code == 429:
                raise ForecaError("Foreca HTTP 429: лимит запросов", status=429)
            if e.code >= 500:
                raise OSError(f"Foreca HTTP {e.code}")
            raise ForecaError(f"Foreca HTTP {e.code}", status=e.code)

    # ------------------------------------------------------------------ #
    # Конвертация в формат информера
    # ------------------------------------------------------------------ #
    def _convert(self, periods, place) -> dict:
        try:
            from zoneinfo import ZoneInfo
            city_tz = ZoneInfo(place.get("tz", "Europe/Moscow"))
        except Exception:
            city_tz = None
        now = datetime.now(city_tz).replace(tzinfo=None) if city_tz else datetime.now()

        def parse_t(s):
            # "2026-09-25T11:00+03:00" -> naive локальное время города
            return datetime.fromisoformat(s).replace(tzinfo=None)

        pts = []
        for it in periods:
            try:
                dt = parse_t(it["time"])
            except (KeyError, ValueError):
                continue
            pts.append({
                "_dt": dt,
                "valid": dt.isoformat(timespec="seconds"),
                "temperature": it.get("temperature"),
                "humidity": None,          # прогноз Foreca влажность не отдаёт
                "pressure": None,          # ... и давление
                "wind_speed": it.get("windSpeed"),
                "wind_direction": it.get("windDir"),
                "precip_prob": it.get("precipProb"),
                "precip_mm": it.get("precipAccum"),
                "symbol": it.get("symbol", ""),
                "source": "foreca",
                "interpolated": False,
            })
        from datetime import timedelta as _td
        horizon = now + _td(hours=self.FORECAST_HOURS)
        pts = [p for p in pts if p["_dt"] <= horizon]
        if not pts:
            raise ForecaError("Foreca: после фильтра 48ч не осталось точек")

        cur = pts[0]
        fact = {
            "temp": (round(cur["temperature"]) if cur["temperature"] is not None else None),
            "icon": self._icon_from_point(cur, now.hour),
            "wind_speed": cur["wind_speed"],
            "wind_angle": cur["wind_direction"],
            "humidity": None,
            "pressure_mm": None,
            "condition": "",
        }

        # hours текущих суток (по локальному времени точек)
        today = now.date()
        hours = [{"hour": str(p["_dt"].hour), "temp": (round(p["temperature"]) if p["temperature"] is not None else None)}
                 for p in pts if p["_dt"].date() == today and p["_dt"].hour >= now.hour]

        # parts: группы по локальному времени точек
        def band(h):
            return "morning" if 6 <= h < 12 else "day" if 12 <= h < 18 else "evening" if 18 <= h < 24 else "night"

        parts = {}
        for name, h0, h1 in (("night", 0, 6), ("morning", 6, 12), ("day", 12, 18), ("evening", 18, 24)):
            sel = [p for p in pts if p["_dt"].date() == today and h0 <= p["_dt"].hour < h1]
            temps = [p["temperature"] for p in sel if p["temperature"] is not None]
            winds = [p["wind_speed"] for p in sel if p["wind_speed"] is not None]
            mid = sel[len(sel) // 2] if sel else None
            parts[name] = {
                "temp_avg": (round(sum(temps) / len(temps)) if temps else None),
                "icon": (self._icon_from_point(mid, mid["_dt"].hour) if mid else "bkn_d"),
                "wind_speed": (round(sum(winds) / len(winds), 1) if winds else None),
                "wind_angle": (mid["wind_direction"] if mid else None),
            }

        return {
            "src": "Foreca",
            "fact": fact,
            "forecasts": [{
                "sunrise": None, "sunset": None,
                "moon_code": _moon_code(),
                "hours": hours,
                "parts": parts,
            }, {"parts": parts}],
            "foreca_points": [{k: v for k, v in p.items() if not k.startswith("_")} for p in pts],
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _icon_from_point(p, local_hour=None):
        """symbol Foreca (d000..d730/n...) + вероятность/объём осадков -> класс информера.
        Реальный справочник (живые данные): префикс d/n + цифры;
        0xx ясно, 1xx-2xx малооблачно/переменно, 3xx-4xx облачно/пасмурно,
        41x-42x морось/гололёд, 5xx дождь, 6xx снег, 7xx гроза."""
        sym = str(p.get("symbol") or "")
        prob = p.get("precip_prob") or 0
        accum = p.get("precip_mm") or 0
        first = sym[:1]
        suffix = "_n" if first == "n" else "_d"
        digits = "".join(ch for ch in sym if ch.isdigit())
        code = int(digits[:3]) if digits else 100
        if 700 <= code < 800:
            return "ovc_ts"
        if 600 <= code < 700:
            return "ovc_sn"
        if 410 <= code < 600:
            return "ovc_ra"
        if (prob is not None and prob >= 40) or (accum and accum > 0.1):
            return "ovc_ra"
        code = int(digits[:3]) if digits else 100
        if code == 0:
            return "skc" + suffix
        if code <= 200:
            return "bkn" + suffix
        return "ovc"

    # ------------------------------------------------------------------ #
    # Кэш локейшенов
    # ------------------------------------------------------------------ #
    def _load_location_cache(self):
        if not self._location_cache_path or not os.path.isfile(self._location_cache_path):
            return
        try:
            with open(self._location_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            self._loc_cache = {k: v for k, v in data.items()
                               if now - v.get("ts", 0) < self.LOCATION_TTL}
        except (OSError, ValueError) as e:
            self.log.warning("Foreca: не удалось прочитать кэш локейшенов: %s", e)

    def _save_location_cache(self):
        if not self._location_cache_path:
            return
        try:
            tmp = self._location_cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._loc_cache, f, ensure_ascii=False)
            os.replace(tmp, self._location_cache_path)
        except OSError as e:
            self.log.warning("Foreca: не удалось сохранить кэш локейшенов: %s", e)


def _moon_code():
    """Фаза луны 0-7 (0 новолуние, 4 полнолуние); локальная копия — изоляция провайдера."""
    ref = datetime(2000, 1, 6, 18, 14)
    days = (datetime.now() - ref).total_seconds() / 86400.0
    frac = (days % 29.530588853) / 29.530588853
    return int(round(frac * 8)) % 8
