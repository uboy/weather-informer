# Weather Informer (Яндекс.Погода в Kiosk Mode)

## О проекте

Автономный погодный информер для старых Android-планшетов и смартфонов, созданный на основе статьи [mysku.club/blog/diy/105101.html](https://mysku.club/blog/diy/105101.html) («Погодный информер из старого телефона»).
Отображает:
* Крупные цифровые часы (секунды, дата, день недели) на всю ширину экрана.
* Текущую погоду (температура, анимированная иконка, min/max дня).
* Ветер (скорость, направление, шкала), влажность (процент, шкала), давление (мм рт. ст., шкала).
* Прогноз погоды на следующие периоды дня (вечер/ночь/утро).
* Астрономический блок: время восхода и заката, полоса светового дня с обратным отсчётом, фаза луны.
* Информационную плашку статуса с понятным описанием сетевых ошибок и времени актуальности данных.

---

## Архитектура и возможности

1. **Горизонтальная 2-колоночная вёрстка (`@media (orientation: landscape)`)**:
   * Верх: гигантские часы (`34vw`) с мигающим двоеточием и крупная дата.
   * Низ (левая колонка): текущая погода, min/max, ветер, влажность, давление.
   * Низ (правая колонка): прогноз на ближайшие периоды, восход/закат, световой день, луна.
2. **Кэширование, оффлайн-работа и почасовой прогноз**:
   * Данные погоды сохраняются в `localStorage` (`weather_cache_data`) и `weather_cache.json`.
   * При сбое связи или лимите 403 информер продолжает работать автономно до 2 суток (48 часов):
     * При сбое более 1 часа текущая температура, ветер, влажность и иконка обновляются каждый час из почасового среза (`forecasts.hours[hour]`).
     * В полночь автоматически происходит календарное переключение на прогноз следующих суток (`forecasts[1]`).
     * Прогнозные карточки (утро/день/вечер/ночь) динамически адаптируются под текущий системный час.
3. **Автоматический fallback на Open-Meteo**:
   * При исчерпании лимита Яндекса или сетевом сбое информер автоматически делает запрос к бесплатному безлимитному **Open-Meteo API**.
   * Данные конвертируются в идентичный формат Яндекса (3 полных суток с почасовыми срезами на 72 часа, периодами, восходом/закатом) со 100% сохранением визуального оформления.
4. **Защита от дезинформации и статусная строка (`#status_bar`)**:
   * Если данные в кэше устарели более чем на 2 суток (48 часов), погодный блок `#weather_body` полностью скрывается, а цифровые часы масштабируются на весь экран.
   * Внизу отображается статусная строка с точным временем последней попытки запроса (с секундами) и текстом ошибки.
5. **Внешний файл конфигурации (`config.json`)**:
   * Все настройки вынесены в `config.json` без необходимости редактировать HTML.
6. **Поддержка локального кэширующего сервера (`server_url`)**:
   * Можно перенаправить запрос на локальный прокси (`caching_server.py`), который делает запросы к Яндексу строго по расписанию (раз в 50 минут) и обслуживает любое количество устройств дома без траты суточного лимита.

---

## Конфигурация (`config.json`)

Файл лежит рядом с `informer.html` (на планшете: `/sdcard/Download/config.json`):

```json
{
  "server_url": "",
  "api": "0d7e54ed-6215-48f0-916f-e234a36cb032",
  "lat": 56.317722,
  "lon": 43.999303,
  "timeout": 3600,
  "max_cache_age_hours": 48,
  "enable_openmeteo_fallback": true,
  "wind_max": 15,
  "pressure_min": 710,
  "pressure_max": 770,
  "port": 8085,
  "cache_interval_minutes": 50
}
```

* `server_url`: адрес локального кэширующего сервера (например `"http://192.168.1.55:8085/weather.json"`). Если строка пустая, информер стучится напрямую в API Яндекса. При отказе локального сервера автоматически пробует прямой доступ к Яндексу.
* `api`: API-ключ Яндекс.Погоды (тариф «Тестовый» / «Погода на вашем сайте»).
* `lat`, `lon`: географические координаты точки наблюдения.
* `timeout`: интервал планового опроса в секундах (по умолчанию 3600 = 1 час).
* `max_cache_age_hours`: допустимый возраст оффлайн-кэша в часах (по умолчанию 48). При превышении блок погоды скрывается.
* `enable_openmeteo_fallback`: включить ли автоматический переход на Open-Meteo при недоступности Яндекса (true/false).

---

## Локальный кэширующий сервер (`caching_server.py`)

Так как бесплатный тариф Яндекс.Погоды ограничен **30 запросами в сутки** (1000 в месяц), при наличии нескольких устройств или перезапусках лимит быстро заканчивается.

Для решения в проекте есть лёгкий сервер `caching_server.py` (Python 3, без внешних зависимостей):

### Запуск сервера

```bash
# В папке Projects/WeatherInformer/:
python3 caching_server.py
```

Сервер:
* Забирает погоду из Яндекса раз в 50 минут (28 запросов в сутки — запас до лимита 30/день).
* Сохраняет кэш на диск (`weather_cache.json`), не делая лишних запросов при перезапусках.
* Раздаёт данные клиентам по адресам:
  * `http://<IP>:8085/weather.json` (погода)
  * `http://<IP>:8085/health` (проверка состояния кэша)
* Если Яндекс вернул 403 или недоступен, сервер автоматически обращается к **Open-Meteo** (бесплатный безлимитный источник) и конвертирует прогноз в формат Яндекса, защищая экран от простоя.

---

## Решение CORS и Fullscreen в Webview Kiosk

Файл настроек на планшете: `/data/data/uk.nktnet.webviewkiosk/shared_prefs/user_settings.xml`:

```xml
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="web_content.home_url">file:///sdcard/Download/informer.html</string>
    <boolean name="web_engine.allow_universal_access_from_file_urls" value="true" />
    <boolean name="web_engine.allow_file_access_from_file_urls" value="true" />
    <boolean name="web_engine.enable_dom_storage" value="true" />
    <boolean name="web_content.allow_local_files" value="true" />
    <string name="appearance.address_bar_mode">HIDDEN</string>
    <string name="appearance.floating_toolbar_mode">HIDDEN</string>
    <string name="appearance.immersive_mode">SYSTEM_BARS</string>
    <string name="appearance.theme">DARK</string>
    <string name="appearance.webview_inset">NONE</string>
    <string name="device.rotation">ROTATION_90</string>
</map>
```

---

## Быстрое развёртывание на планшете

```bash
# 1. Залить файлы на планшет
adb push informer.html /sdcard/Download/informer.html
adb push config.json /sdcard/Download/config.json
adb push weather_cache.json /sdcard/Download/weather_cache.json

# 2. Установить Webview Kiosk
adb install uk.nktnet.webviewkiosk.apk

# 3. Применить настройки и права
adb push kiosk_user_settings.xml /data/local/tmp/user_settings.xml
adb shell "su -c '
cp /data/local/tmp/user_settings.xml /data/data/uk.nktnet.webviewkiosk/shared_prefs/user_settings.xml
chown \$(stat -c %u:%g /data/data/uk.nktnet.webviewkiosk) /data/data/uk.nktnet.webviewkiosk/shared_prefs/user_settings.xml
chmod 660 /data/data/uk.nktnet.webviewkiosk/shared_prefs/user_settings.xml
restorecon /data/data/uk.nktnet.webviewkiosk/shared_prefs/user_settings.xml
rm /data/local/tmp/user_settings.xml
'"

# 4. Настроить надёжный NTP и часовой пояс (защита от рассинхронизации и дрейфа RTC)
adb shell settings put global auto_time 1
adb shell settings put global auto_time_zone 0
adb shell setprop persist.sys.timezone Europe/Moscow
adb shell settings put global ntp_server ru.pool.ntp.org
adb shell settings put global ntp_timeout 5000
adb shell settings put global auto_time 0 && adb shell settings put global auto_time 1
adb shell "su -c 'date -s @\$(date +%s) && hwclock -w' 2>/dev/null || true"

# 5. Дать права на чтение хранилища и запустить
adb shell pm grant uk.nktnet.webviewkiosk android.permission.READ_EXTERNAL_STORAGE
adb shell am start -n uk.nktnet.webviewkiosk/.MainActivity
```

---

## Подключенные устройства

| # | Модель | IP (WiFi ADB) | Android | Разрешение / Соотношение | Ориентация | Особенности |
|---|--------|---------------|---------|---------------------------|------------|-------------|
| 1 | **Digma Plane 7514S** | `192.168.1.42:5555` | 6.0 | 1280×800 (16:10) | `ROTATION_90` | Адаптивная верстка (высокие экраны, крупный шрифт 27.5vw) |
| 2 | **Oysters T74HMi LTE** | `192.168.1.52:5555` | 6.0 | 1024×600 (16:9) | `ROTATION_270` | Разворот 180° под кабель, клиренс под статус-бар |
| 3 | **4GOOD Light AT200** | `192.168.1.30:5555` | 6.0 | 1280×800 (16:10) | `ROTATION_90` | Очищено 1.1 ГБ кэша, отключен GMS, устранен димминг |
| 4 | **IRBIS TZ175** | `192.168.1.31:5555` | 7.0 | 1024×600 (16:9) | `ROTATION_90` | Очищено bloatware, отключены GMS/Play Store, RAM оптимизирована |

