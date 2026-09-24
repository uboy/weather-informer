# Weather Informer — парк погодных планшетов-киосков

Локальный кэширующий сервер погоды + веб-страница киоска для парка Android-планшетов.
Сервер ходит в Яндекс.Погоду **раз в 90 минут** (квота бесплатного тарифа 30 запросов/сутки),
кэширует результат и раздаёт неограниченному числу планшетов. При сбое Яндекса — автоматическая
цепочка фолбэков: **OpenWeatherMap → Open-Meteo → wttr.in → 7timer** (и на планшете, если
пропал сервер: OWM → wttr.in). Планшеты никогда не ходят в Яндекс напрямую.

## Архитектура

```
Яндекс.Погода (90 мин) ──┐
OpenWeatherMap (fallback)┤
Open-Meteo (fallback) ───┤→ caching_server.py :8085 ──→ планшеты (webview kiosk)
wttr.in (fallback) ──────┤      ├─ /weather.json — данные
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

**Секреты:** реальный `config.json` (ключ) в .gitignore — в репозитории только шаблоны.

## Выбор города (на планшете)

В меню киоска (☰) секция **«Город»**:
- **Поиск города…** → «Найти город» → подсказки (Nominatim через сервер: `GET /geocode?q=…`, при блокировке — через прокси `om_proxy`) → тап по подсказке.
- **Определить автоматически (GPS)** — геолокация устройства. Требует: в WebViewKiosk `device.allow_location=true` (настройка приложения или правка `user_settings.xml` под root) + `pm grant …ACCESS_FINE_LOCATION` + включённая служба локации; на планшетах без сетевой геолокации (GMS) работает только по GPS-чипу.
- **Сбросить город** — возврат к координатам из `config.json`.

Выбранный город хранится в localStorage (`city_name/city_lat/city_lon`) и **переиспользуется всеми источниками** (и сервером — через `GET /weather.json?lat=&lon=`, и прямыми). Город всегда показан в статус-баре внизу. Имя по умолчанию — поле `city_name` в конфиге планшета.

Обратный геокодинг для GPS: `GET /reverse?lat=&lon=` (тот же Nominatim). Кэш сервера ведётся на каждую точку отдельно (до 8 городов), интервал обновления общий.

## Эксплуатация

- Здоровье: `curl http://<сервер>:8085/status` → `{"status":"ok","cache_age_minutes":N}`
- Логи: `journalctl --user -u weather-informer -f`
- Статистика источников: `weather_stats.csv` (кто, когда, температура) + `stats_report.py`
- Расход квоты: ~16 запросов/сутки при интервале 90 мин (менять через `cache_interval_minutes`)
- Рестарты безопасны: кэш на диске хранит время последнего запроса — свежий кэш не перезапрашивается
- Дубли экземпляров невозможны: порт биндится до старта фоновых потоков, вторая копия умирает мгновенно
