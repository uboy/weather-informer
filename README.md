# Weather Informer — парк погодных планшетов-киосков

Локальный кэширующий сервер погоды + веб-страница киоска для парка Android-планшетов.
Сервер ходит в Яндекс.Погоду **раз в 90 минут** (квота бесплатного тарифа 30 запросов/сутки),
кэширует результат и раздаёт неограниченному числу планшетов. Цепочка фолбэков сервера:
**OpenWeatherMap → Open-Meteo → Gismeteo → wttr.in → 7timer**. Клиентский фолбэк при пропавшем
сервере — по приоритету безлимитных источников: **OWM → Open-Meteo → 7timer → wttr.in**,
и только затем прямой Яндекс (тратит общую квоту 30/сутки, поэтому последний).

## Архитектура

```
Яндекс.Погода (90 мин) ──┐
OpenWeatherMap (fallback)┤
Open-Meteo (fallback) ───┤→ caching_server.py :8085 ──→ планшеты (webview kiosk)
Gismeteo (fallback) ─────┤      ├─ /weather.json[?lat=&lon=&source=gismeteo]
wttr.in (fallback) ──────┤      ├─ /geocode?q= / /reverse?lat=&lon= — город
7timer (fallback) ───────┘      ├─ /status — здоровье, возраст кэша
                                 └─ weather_stats.csv — лог источников
```

Клиент — статическая страница `informer.html`, лежащая локально на каждом планшете
(`/sdcard/Download/informer.html`), данные тянет из `config.json` рядом (server_url).
Плюс локальный кэш в localStorage — переживает недоступность сервера.

## Состав репозитория

| Файл | Что |
|---|---|
| `caching_server.py` | сервер: кэш, фолбэк-цепочка, статистика, защита от дублей |
| `informer.html` | страница киоска (деплоится на планшеты) |
| `config.example.json` | шаблон конфига сервера (реальный `config.json` в .gitignore!) |
| `tablets/config.example.json` | шаблон конфига планшета |
| `install/weather-informer.service` | systemd --user юнит |
| Цвет текста киоска | меню киоска → «Цвет» (белый/зелёный/янтарный/голубой/розовый), либо `text_color` в config.json планшета. По умолчанию белый |
| `install/install-service.sh` | установка сервиса (linger, автостарт) |
| `deploy/informer-to-tablet.sh` | деплой страницы на один планшет |
| `deploy/all-tablets.sh` | деплой на весь парк (список в `deploy/tablets.list`) |
| `gismeteo_provider.py` | изолированный провайдер Gismeteo (без токена); endpoint/XML меняется только внутри него |
| `tests/test_gismeteo.py` | unittest провайдера (HTTP замокан) |
| `tests/icon-test.js` | тесты значков и конвертаций клиента (`node tests/icon-test.js`) |
| `host-watchdog/` | хостовой adb-watchdog парка (systemd --user таймер, 3 мин) |
| `host-watchdog/device-42/` | копия скриптов на планшете .42 (`/data/adb/service.d/`) |
| `gismeteo_cities.json` | (создаётся при работе) долговременный кэш координаты → city_id |

## Развёртывание сервера

```bash
cp config.example.json config.json   # вписать ключ Яндекса и lat/lon
./install/install-service.sh
```

Скрипт ставит юнит в `~/.config/systemd/user/`, включает `linger` (работа без логина),
запускает и проверяет `GET /status`. Логи: `journalctl --user -u weather-informer -f`.

## Развёртывание планшета (root/adb)

1. Установить kiosk-приложение (WebViewKiosk) и указать home = `file:///sdcard/Download/informer.html`
2. Положить конфиг: `adb push tablets/config.example.json /sdcard/Download/config.json` (вписать IP сервера; поле `api` — любая строка-заглушка, ключ на планшетах не нужен)
3. Задеплоить страницу: `./deploy/informer-to-tablet.sh <serial|ip:5555>`
4. Автостарт adb-over-wifi после загрузки — скрипт в Magisk `/data/adb/service.d/99-adbtcp.sh`:

```sh
#!/system/bin/sh
setprop persist.adb.tcp.port 5555
setprop service.adb.tcp.port 5555
stop adbd; start adbd
( sleep 120; while true; do
    if [ "$(getprop service.adb.tcp.port)" != "5555" ]; then
      setprop persist.adb.tcp.port 5555; setprop service.adb.tcp.port 5555
      stop adbd 2>/dev/null; start adbd
    fi
    sleep 300
  done ) &
```

Если `adb connect` показывает offline, а планшет работает — протухла сессия adb-сервера хоста:
`adb kill-server && adb connect <ip>:5555`.

## Ключи API

| Источник | Ключ | Как получить |
|---|---|---|
| Яндекс.Погода | нужен | https://developer.tech.yandex.ru/services/ → «Погода» → бесплатный тариф (30 запросов/сутки). Ключ в заголовок `X-Yandex-Weather-Key` |
| OpenWeatherMap | нужен (опционально, fallback) | https://home.openweathermap.org/api_keys (бесплатный тариф). Параметры `openweathermap_api_key` + `enable_openweathermap_fallback` в конфиге сервера и планшета |
| Open-Meteo | не нужен | https://open-meteo.com/ (безлимитно, fallback) |
| wttr.in | не нужен | https://wttr.in/:help |
| 7timer | не нужен | http://www.7timer.info/ |
| Gismeteo | не нужен (внутренний inform-service) | `services.gismeteo.ru` → фолбэк `.net`. **Не официальный API** (`api.gismeteo.net/v4` требует токен) — формат XML может измениться; вся работа изолирована в `gismeteo_provider.py` |

**Секреты:** реальный `config.json` (ключ) в .gitignore — в репозитории только шаблоны.

## Политика данных (без фейков)

Конвертеры **не подставляют выдуманные значения**. Если источник поле не отдаёт —
в JSON уходит `null`, а информер показывает «—» и сбрасывает полосу/стрелку
(значения не «застывают» от предыдущего источника при переключении). Легитимные
агрегации (средний ветер части из hourly, выдержка ближайшей 3-часовой точки
в почасовике — без интерполяции) — разрешены. Фаза луны считается, а не константа.

Покрытие полей по источникам (проверено против живых API):

| Поле | Яндекс | OWM | Open-Meteo | Gismeteo | wttr.in | 7timer |
|---|---|---|---|---|---|---|
| температура | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| влажность | ✓ | ✓ | hourly[hIdx] | ✓ | ✓ | rh2m (строка «46%») |
| давление | из `info.def_pressure_mm` | ✓ | hourly[hIdx] | ✓ | ✓ | **нет** → null |
| ветер (скорость) | ✓ | ✓ | ✓ | ✓ | ✓ | Бофорт → м/с |
| ветер (направление) | ✓ | ✓ | ✓ | ✓ (румб→°) | ✓ | ✓ (румб→°) |
| восход/закат | ✓ | ✓ | ✓ | ✓ (risem/setm, мин) | ✓ | **нет** → null |

Данные Gismeteo — 3-часовые точки; исходные точки сохраняются в ответе
(`gismeteo_points[]`, `"interpolated": false`), почасовик — выдержка ближайшей точки.

## Ground truth (наблюдения для проверки прогнозов)

| Физическая станция | Канал | Статус |
|---|---|---|
| Нижний Новгород WMO 27459 | **OGIMET SYNOP** (`/observations/fetch`, raw в `synop_raw`) | ✅ основной (t/давление/осадки, каждые 3 ч, UTC→МСК) |
| Нижний Новгород WMO 27459 | Foreca observations | ✅ контроль (тот же физический датчик) |
| Strigino (аэропорт) | Foreca / METAR | ✅ независимая станция |
| Volzskaya GMO, Sergac, Arzamas, Krasnye Baki | Foreca | ✅ региональный контроль (осадки — только по нескольким станциям) |
| WMO 27459 через Meteostat | — | ❌ 100% модель DWD MOSMIX, наблюдений нет |
| ISD 274590-99999 | NOAA | 🟡 архив до 08.2025; оперативно — GHCNh (исследовать) |

Наблюдения копятся в `forecast.db` (таблица `observations`, колонка `phys_station`
различает физические датчики; raw SYNOP — `synop_raw`). OGIMET SYNOP хранится
в исходном виде. Влажность станция 27459 не передаёт — humidity-эталон только
из Foreca-станций.

Проверка точности: `GET /accuracy?provider=Gismeteo&lead_hours=3&phys=27459&window=1.5`
(MAE температуры; `phys` — фильтр физической станции, `window` — допуск часов).

## Кэши и лимиты

| Что | TTL / лимит |
|---|---|
| Погода сервера (на точку) | `cache_interval_minutes` (по умолчанию 90 мин) |
| Точек в памяти (`LOCATION_CACHE`) | до 8; город по умолчанию не эвиктится никогда |
| Прогноз Gismeteo (в провайдере) | 20 мин |
| Кэш city_id Gismeteo (`gismeteo_cities.json`) | 30 дней |
| Ключ Яндекса | ~16 запросов/сутки при 90-мин интервале; прямой Яндекс с планшетов — та же квота |

Запрос `GET /weather.json?source=gismeteo` отдаёт **только** Gismeteo для точки
(свой ключ кэша, Яндекс не тратится). Без `source` — обычная цепочка.

## Выбор города (на планшете)

В меню киоска (☰) секция **«Город»**:
- **Поиск города…** → «Найти город» → подсказки (Nominatim через сервер: `GET /geocode?q=…`, при блокировке — через прокси `om_proxy`) → тап по подсказке.
- **Определить автоматически (GPS)** — геолокация устройства. Требует: в WebViewKiosk `device.allow_location=true` (настройка приложения или правка `user_settings.xml` под root) + `pm grant …ACCESS_FINE_LOCATION` + включённая служба локации; на планшетах без сетевой геолокации (GMS) работает только по GPS-чипу.
- **Сбросить город** — возврат к координатам из `config.json`.

Выбранный город хранится в localStorage (`city_name/city_lat/city_lon`) и **переиспользуется всеми источниками** (и сервером — через `GET /weather.json?lat=&lon=`, и прямыми). Город всегда показан в статус-баре внизу. Имя по умолчанию — поле `city_name` в конфиге планшета.

Обратный геокодинг для GPS: `GET /reverse?lat=&lon=` (тот же Nominatim). Кэш сервера ведётся на каждую точку отдельно (до 8 городов), интервал обновления общий.

## Тесты (обязательный прогон перед деплоем)

```bash
bash tests/run-all.sh
```

| Файл | Что покрывает |
|---|---|
| `tests/icon-test.js` | icon_daynight, парность _d/_n, вокабуляр иконок ⊆ CSS |
| `tests/converters-test.js` | **энд-ту-енд клиентские конвертеры** на фикстурах API (OWM/OM/7timer/wttr/Foreca): структура, завтрашние parts, ночные иконки, честные null; все регрессии раундов ревью здесь как векторы (wttr OOB, Foreca TDZ, tzshift-скос, дневная иконка ночи) |
| `tests/test_gismeteo.py` | GismeteoProvider: парсинг XML, хост-фолбэк, кэши, 48ч-фильтр |
| `tests/test_foreca_provider.py` | ForecaProvider: конвертация, иконки по symbol, кэш локейшенов, observations |
| `tests/test_synop.py` | SYNOP-декодер: температура/давление/шкала осадков WMO, ветряные группы ≠ осадки, секция 3 |
| `tests/test_accuracy.py` | accuracy_query: lead в ЧАСАХ, окно ловит METAR :30, регистр, phys-фильтр, negative-lead |

**Правило (вывод из 7 раундов ревью): каждый раунд ловил регрессию в непротестированном коде.** Новый источник = фиксстура + векторы в converters-test; правка конвертера/декодера = прогон run-all; `deploy/all-tablets.sh` гоняет тесты перед деплоем и падает при красном.

## Эксплуатация

- Здоровье: `curl http://<сервер>:8085/status` → `{"status":"ok","cache_age_minutes":N}`
- Логи: `journalctl --user -u weather-informer -f`
- Статистика источников: `weather_stats.csv` (кто, когда, температура) + `stats_report.py`
- Расход квоты: ~16 запросов/сутки при интервале 90 мин (менять через `cache_interval_minutes`)
- Рестарты безопасны: кэш на диске хранит время последнего запроса — свежий кэш не перезапрашивается
- Дубли экземпляров невозможны: порт биндится до старта фоновых потоков, вторая копия умирает мгновенно
