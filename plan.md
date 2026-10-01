# План реализации (Plan): Локальный веб-сервер на планшете, простое развёртывание и интеграция Gismeteo API

## 1. Обзор архитектуры решения

Решение состоит из 3 ключевых частей:

```
+-----------------------------------------------------------------------------------+
| 1. Серверная часть (caching_server.py на домашнем сервере / ПК)                  |
|  - Поддержка Gismeteo API Token (X-Gismeteo-Token) + кэширование (защита квоты)   |
|  - Раздача /weather.json, /api/weather/gismeteo, переключатель primary_source     |
|  - Страница развёртывания /install с QR-кодом для планшета (скачивание APK)      |
+-----------------------------------------------------------------------------------+
                                         │  (локальная сеть Wi-Fi)
                                         ▼
+-----------------------------------------------------------------------------------+
| 2. Планшет (Android Kiosk & Web Server)                                           |
|  - Встроенный легковесный HTTP-сервер на порту 8080                               |
|  - Kiosk WebView: открывает http://127.0.0.1:8080/ (без проблем с file:// и CORS) |
|  - Веб-интерфейс настроек (/settings) для управления с телефона/ПК              |
|  - Загрузчик файлов (/upload) для обновления HTML/JS/CSS по воздуху               |
+-----------------------------------------------------------------------------------+
                                         ▲
                                         │  (браузер телефона)
+-----------------------------------------------------------------------------------+
| 3. Управление со смартфона (телефон в той же Wi-Fi сети)                         |
|  - Открытие http://<IP_планшета>:8080/settings: смена координат, ключей, режимов |
|  - Открытие http://<IP_планшета>:8080/upload: загрузка файлов без кабеля и ADB    |
+-----------------------------------------------------------------------------------+
```

---

## 2. Поэтапный план реализации

> [!IMPORTANT]
> **Этап 0 обязателен перед Этапом 1.** Пропуск подготовительного рефакторинга создаёт угрозу безопасности с первого коммита (вердикт ревью 2: CHANGES REQUESTED → MUST-FIX #1–5).

### Этап 0: Подготовительный рефакторинг (БЛОКЕР — выполнить до Этапа 1)

1. **Безопасность секретов в git**:
   - Добавить `config.json` в `.gitignore` — в нём хранятся реальные API-ключи (Яндекс, Foreca JWT, OWM).
   - Создать `config.example.json` с ключами-заглушками (`"xxxxxxxx-xxxx-..."`) для документации.

2. **Расширение `sanitize_secrets()` в `caching_server.py`**:
   - Строка 188: добавить `"gismeteo_api_key"` в список ключей конфига.
   - После строки 196: добавить `re.sub(r'(X-Gismeteo-Token[:=]\s*)[^\s,\'"]+', r'\1******', s, flags=re.IGNORECASE)`.
   - Добавить тест `test_sanitize_gismeteo_token()` в `tests/test_fallback.py`.

3. **Расширение `DEFAULT_CONFIG` и `config.json`**:
   - Добавить `"gismeteo_api_key": ""` и `"primary_source": "yandex"` в `DEFAULT_CONFIG` (строки 130–158).
   - Добавить те же ключи в `config.json`.

4. **Создание отдельного `gismeteo_v2_provider.py`** *(не расширять существующий `GismeteoProvider` — SRP)*:
   - Класс `GismeteoV2Provider` с интерфейсом `get_weather(lat, lon) -> dict`.
   - Эндпоинты: `/v2/search/cities/`, `/v2/weather/forecast/aggregate/`.
   - Маппинг иконок v2 (c1_d→skc_d и т.д.), давления (mm_hg_atm→int), ветра (scale_8→градусы).
   - Исключение `GismeteoV2QuotaError` для HTTP 401/403/429.
   - В `caching_server.py`: `GISMETEO_V2 = GismeteoV2Provider(api_key=...)` рядом с `GISMETEO`.

5. **Рефакторинг `_fetch_weather_locked()` в `caching_server.py`**:
   - Декомпозиция монолитной 216-строчной функции: `_try_yandex()`, `_try_gismeteo_v2()`, `_try_gismeteo_xml()`.
   - Модульная переменная `gismeteo_v2_quota_blocked_until: float = 0.0` (суточный кулдаун).
   - Функция `_next_midnight_utc() -> float`.
   - Обобщение `yandex_quota_blocked` → `primary_quota_blocked` для поддержки `primary_source`.
   - Ключи кэша: `:gismeteo` для v2 REST, `:gismeteo_xml` для XML inf_chrome.
   - `valid_sources` (строка 1785): добавить `"gismeteo_xml"`.

6. **Увеличить `MAX_LOCATIONS`** с 8 до 16 (`caching_server.py`, строка 48).

7. **Создать `tests/test_gismeteo_v2.py`** и расширить `tests/run-all.sh`:
   - Тесты: парсинг cities JSON, forecast aggregate, маппинг иконок, давление, ветер, quota 401/403/429, пустой ответ.
   - `run-all.sh`: добавить `echo "=== python gismeteo_v2 ==="; python3 tests/test_gismeteo_v2.py 2>&1 | tail -1`.

### Этап 1: Завершение интеграции Gismeteo API (Backend & Client)
1. **Завершение `gismeteo_v2_provider.py`** (полный JSON-парсер, кэш city_id, логика TTL).
2. **Обновление `caching_server.py`**:
   - `primary_source: "gismeteo"`: `GISMETEO_V2` вызывается первым, Яндекс — резерв.
   - Раздача по `/weather.json` и `/api/weather/gismeteo`.
   - Статистика в `forecast.db` и `weather_stats.csv`.
3. **Обновление `informer.html`**:
   - Поле `gismeteo_api_key` и прямой режим через `/proxy/weather` планшетного сервера (строго ES5).

### Этап 2: Веб-сервер на планшете (Web UI настроек, показ информера, загрузка файлов)
1. **Разработка легковесного веб-сервера (NanoHTTPD 2.3.1) в рамках Android-проекта `android/`**:
   - Foreground Service с `WifiLock(WIFI_MODE_FULL_HIGH_PERF)` и `WakeLock`.
   - TLS для `/proxy/weather` на Android 5–6: **Conscrypt** (`org.conscrypt:conscrypt-android:2.5.2` + `Security.insertProviderAt(Conscrypt.newProvider(), 1)` в `Application.onCreate()`).
   - Маршруты: `/`, `/informer.html`, `/settings`, `/api/settings`, `/upload`, `/api/upload`, `/proxy/weather`, `/health`.
   - `/api/upload`: санитизация `getName()`, `getCanonicalPath()`, whitelist расширений, лимит 5 МБ.
   - `/proxy/weather`: жёсткий whitelist доменов (`api.gismeteo.net`, `api.weather.yandex.ru`, `api.open-meteo.com`).
2. **Kiosk WebView**: `http://127.0.0.1:8080/`, `FLAG_KEEP_SCREEN_ON`, immersive mode `SYSTEM_UI_FLAG_IMMERSIVE_STICKY`.

### Этап 3: Упрощение развёртывания (Zero-ADB со смартфона)
1. **APK «Weather Informer Kiosk & Server»** (`minSdkVersion 21`, `targetSdkVersion 28`):
   - Встроенный сервер NanoHTTPD, Kiosk WebView, файлы в `assets/`, автостарт `RECEIVE_BOOT_COMPLETED`.
   - Исходный код в `android/` с автономным `build.gradle`.
2. **Страница `/install`** в `caching_server.py`: QR-код и раздача APK по `/informer.apk`.

### Этап 4: Верификация и документация
1. Полный прогон `tests/run-all.sh` (включая `test_gismeteo_v2.py`, `test_sanitize_gismeteo_token`, тесты `primary_source`).
2. Обновление `README.md`: сценарий Zero-ADB, настройка ключа Gismeteo.

---

## 3. Критерии приемки (Definition of Done)
1. `tests/run-all.sh` выполняется со 100% успехом (включая новые тесты Gismeteo API).
2. Кэширующий сервер успешно обрабатывает ключ Gismeteo, сохраняет данные в кэш и отдает их клиентам.
3. Информер может работать как через кэширующий сервер, так и напрямую через API Gismeteo с указанным ключом.
4. На планшете функционирует веб-сервер с возможностью:
   - просмотра информера по HTTP;
   - редактирования настроек через веб-интерфейс с телефона;
   - загрузки файлов на планшет по воздуху.
5. Готов понятный сценарий развёртывания информера на планшет с телефона без кабелей и консольного ADB.
6. `tests/test_gismeteo_v2.py` содержит тесты JSON-парсера, маппинга иконок, давления и кулдауна квоты; `run-all.sh` включает их прогон.
7. `config.json` добавлен в `.gitignore`; `config.example.json` существует в репозитории.

