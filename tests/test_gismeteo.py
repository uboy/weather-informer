#!/usr/bin/env python3
"""Unit-тесты GismeteoProvider (HTTP замокан через GismeteoProvider._http_get)."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gismeteo_provider import GismeteoError, GismeteoProvider

CITIES_XML = (
    '<document>'
    '<item id="12975" lat="56.22" lng="43.78" tzone="180" n="Нижний Новгород / Стригино" '
    'kind="A" distance="18.324" country_name="Россия"/>'
    '<item id="4355" lat="56.33" lng="43.99" tzone="180" n="Нижний Новгород" '
    'kind="T" distance="1.039" country_name="Россия"/>'
    '</document>'
)

CITIES_EMPTY = '<document></document>'

NOW_UTC = datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _pts():
    """Точки с 3-часовым шагом: от 00:00 UTC сегодня до now+54ч (как в реальном XML)."""
    out = []
    t = NOW_UTC.replace(hour=0, minute=0, second=0, microsecond=0)
    end = NOW_UTC + timedelta(hours=54)
    while t <= end:
        h = t.hour
        tod_temp = 10 + (h - 3) % 14
        out.append(
            f'<forecast valid="{t.strftime("%Y-%m-%dT%H:%M:%S")}" tod="1">'
            f'<values t="{tod_temp}" p="752" ws="3" wd="2" hum="60" cl="1" pt="0" pr="0" '
            f'ts="0" icon="d.c1" descr="Малооблачно"/></forecast>'
        )
        t += timedelta(hours=3)
    return "".join(out)


def _forecast_xml(fact_valid, risem="352", setm="1078", tzone="180"):
    return (
        '<weather><location id="4355" name="Нижний Новгород" lat="56.33" lng="43.99" '
        f'tzone="{tzone}" cur_time="{NOW_UTC.strftime("%Y-%m-%dT%H:%M:%S")}">'
        f'<fact valid="{fact_valid}" tod="4" risem="{risem}" setm="{setm}">'
        '<values t="18" p="759" ws="2" wd="4" hum="56" cl="1" pt="0" pr="0" ts="0" '
        'icon="n.c2" descr="Малооблачно"/></fact>'
        f'<day date="{NOW_UTC.strftime("%Y-%m-%d")}" tmin="10" tmax="20">{_pts()}</day>'
        '</location></weather>'
    )


BAD_XML = b'<document><item id='
NOT_XML = b'{"json": "not xml"}'


class FakeHttp:
    """Подмена HTTP-слоя: handler(url) -> bytes (может бросать исключения)."""

    def __init__(self, provider, handler):
        self.provider = provider
        self.handler = handler
        self.calls = []

    def __enter__(self):
        self._orig = self.provider._http_get

        def fake_get(url, timeout):
            self.calls.append(url)
            result = self.handler(url)
            if isinstance(result, Exception):
                raise result
            return result

        self.provider._http_get = staticmethod(fake_get)
        return self

    def __exit__(self, *exc):
        self.provider._http_get = self._orig


def default_handler(url):
    """Роутер по умолчанию: cities/forecast по path, отказ .ru."""
    if "gismeteo.ru/" in url:
        raise OSError("connection refused (.ru host down)")
    if "/cities/" in url:
        return CITIES_XML.encode()
    if "/forecast/" in url:
        return _forecast_xml("2026-09-24T18:00:00").encode()
    raise AssertionError("неожиданный url: " + url)


def make_provider(tmpdir=None, geocode=None):
    """Провайдер с уникальным файлом дискового кэша на тест."""
    d = tempfile.mkdtemp(prefix="gismtest_")
    return GismeteoProvider(
        geocode_fn=geocode,
        city_cache_path=str(Path(d) / "cities.json"),
    )


class GismeteoTests(unittest.TestCase):
    def test_1_parse_cities(self):
        """Разбор /cities/: приоритет kind=T над аэропортом, поля на месте."""
        p = make_provider("/tmp/opencode")
        items = p._parse_cities(CITIES_XML.encode())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "4355")  # kind=T, а не аэропорт 12975
        self.assertEqual(items[0]["name"], "Нижний Новгород")
        self.assertEqual(items[0]["tzone_min"], 180)

    def test_2_parse_forecast(self):
        """Разбор /forecast/: fact + точки, tzone, sun times."""
        p = make_provider("/tmp/opencode")
        parsed = p._parse_forecast(_forecast_xml("2026-09-24T18:00:00").encode())
        self.assertEqual(parsed["name"], "Нижний Новгород")
        self.assertEqual(parsed["tzone_min"], 180)
        self.assertEqual(parsed["fact"]["t"], "18")
        self.assertGreaterEqual(len(parsed["points"]), 16)
        rise, sett = p._sun_times(parsed)
        self.assertEqual((rise, sett), ("05:52", "17:58"))

    def test_3_bad_xml(self):
        """Некорректный XML -> GismeteoError, а не исключение парсера."""
        p = make_provider("/tmp/opencode")
        with self.assertRaises(GismeteoError):
            p._parse_cities(BAD_XML)
        with self.assertRaises(GismeteoError):
            p._parse_forecast(NOT_XML)

    def test_4_empty_cities(self):
        """Пустой результат /cities/ -> понятная ошибка «не найден»."""
        p = make_provider("/tmp/opencode")
        with FakeHttp(p, lambda u: CITIES_EMPTY.encode()):
            with self.assertRaises(GismeteoError) as ctx:
                p.get_weather(latitude=1.0, longitude=2.0)
        self.assertIn("не найден", str(ctx.exception))

    def test_5_host_fallback(self):
        """Первый хост (.ru) упал — запрос ушёл на второй (.net)."""
        p = make_provider("/tmp/opencode")
        with FakeHttp(p, default_handler) as fh:
            data = p.get_weather(latitude=56.3269, longitude=44.0059)
        self.assertTrue(any("gismeteo.ru/" in u for u in fh.calls), "первый хост должен был вызываться")
        self.assertTrue(any("gismeteo.net" in u for u in fh.calls), "фолбэк на .net не выполнен")
        self.assertEqual(data["fact"]["temp"], 18)

    def test_6_forecast_48h_filter(self):
        """Дальше 48ч — отсекается; прошедшие часы СЕГОДНЯ (для parts) — остаются."""
        p = make_provider()
        with FakeHttp(p, default_handler):
            data = p.get_weather(latitude=56.3269, longitude=44.0059)
        pts = data["gismeteo_points"]
        self.assertGreater(len(pts), 8)
        now = datetime.utcnow()
        today_start_utc = (now + timedelta(minutes=180)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=180)
        for pt in pts:
            dt = datetime.strptime(pt["valid_utc"], "%Y-%m-%dT%H:%M:%S")
            self.assertLessEqual(dt, now + timedelta(hours=48, minutes=1))
            self.assertGreaterEqual(dt, today_start_utc)
            self.assertFalse(pt["interpolated"])
            self.assertEqual(pt["source"], "gismeteo")
        # вечером части «днём/утром» не заглушки: содержат реальные дневные температуры
        parts = data["forecasts"][0]["parts"]
        self.assertNotEqual(parts["day"]["temp_avg"], 0)

    def test_7_timezone(self):
        """tzone учитывается: локальное время точки = UTC + tzone (мин)."""
        p = make_provider("/tmp/opencode")
        with FakeHttp(p, default_handler):
            data = p.get_weather(latitude=56.3269, longitude=44.0059)
        pt = data["gismeteo_points"][0]
        utc_dt = datetime.strptime(pt["valid_utc"], "%Y-%m-%dT%H:%M:%S")
        loc_dt = datetime.strptime(pt["valid_local"], "%Y-%m-%dT%H:%M:%S")
        delta = (loc_dt - utc_dt).total_seconds()
        self.assertEqual(int(delta), 180 * 60)
        # и восход из risem (минуты) конвертирован в строку
        self.assertRegex(data["forecasts"][0]["sunrise"], r"^\d{2}:\d{2}$")

    def test_8_by_coordinates(self):
        """Работа по координатам: cities -> forecast -> структура формата."""
        p = make_provider("/tmp/opencode")
        with FakeHttp(p, default_handler) as fh:
            data = p.get_weather(latitude=56.3269, longitude=44.0059)
            self.assertTrue(any("/cities/" in u for u in fh.calls))
            self.assertTrue(any("/forecast/" in u and "city=4355" in u for u in fh.calls))
        self.assertIn("fact", data)
        self.assertIn("forecasts", data)
        self.assertEqual(data["src"], "Gismeteo")
        self.assertIn("parts", data["forecasts"][0])
        self.assertIn("hours", data["forecasts"][0])
        # координаты -> city_id закэширован: второй вызов без cities-запроса
        with FakeHttp(p, default_handler) as fh:
            p.get_weather(latitude=56.3269, longitude=44.0059)
            self.assertFalse(any("/cities/" in u for u in fh.calls))
        # кэш city_id переживает пересоздание провайдера (диск)
        p2 = GismeteoProvider(geocode_fn=None, city_cache_path=p._city_cache_path)
        with FakeHttp(p2, default_handler) as fh:
            p2.get_weather(latitude=56.3269, longitude=44.0059)
            self.assertFalse(any("/cities/" in u for u in fh.calls))

    def test_9_by_city_name(self):
        """Работа по названию: геокодер -> координаты -> cities -> forecast."""
        calls = {"geo": 0}

        def geocode(name):
            calls["geo"] += 1
            return [{"name": "Нижний Новгород", "lat": 56.3269, "lon": 44.0059}]

        p = make_provider("/tmp/opencode", geocode=geocode)
        with FakeHttp(p, default_handler):
            data = p.get_weather(city="Нижний Новгород")
        self.assertEqual(calls["geo"], 1)
        self.assertEqual(data["fact"]["temp"], 18)

    def test_10_invalid_coords_and_missing_args(self):
        """Валидация координат и обязательность аргументов."""
        p = make_provider("/tmp/opencode")
        with self.assertRaises(GismeteoError):
            p.get_weather(latitude=95.0, longitude=44.0)
        with self.assertRaises(GismeteoError):
            p.get_weather(latitude=56.0, longitude=999.0)
        with self.assertRaises(GismeteoError):
            p.get_weather(latitude=56.0)  # без долготы
        with self.assertRaises(GismeteoError):
            p.get_weather()  # ни города, ни координат

    def test_11_http_403_429(self):
        """403/429 -> GismeteoError без бесконечных повторов."""
        import urllib.error

        p = make_provider("/tmp/opencode")

        def http403(url, timeout):
            raise GismeteoError("Gismeteo HTTP 403: доступ ограничен")

        orig = GismeteoProvider._http_get
        GismeteoProvider._http_get = staticmethod(http403)
        try:
            with self.assertRaises(GismeteoError):
                p.get_weather(latitude=56.3269, longitude=44.0059)
        finally:
            GismeteoProvider._http_get = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
