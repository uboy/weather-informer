# Архитектурное ревью №2 (Architectural Review 2)
## Weather Informer: Gismeteo API v2, primary_source, кэш, TLS, тестирование

**Дата ревью:** 01 октября 2026 г.  
**Проект:** `/home/dmazur/services/weather-informer`  
**Ревьюер:** Principal Systems Architect & Systems Reviewer  
**Предыдущее ревью:** [ARCHITECT_REVIEW.md](file:///home/dmazur/services/weather-informer/ARCHITECT_REVIEW.md)  
**Статус:** **CHANGES REQUESTED**

> [!IMPORTANT]
> Данное ревью является **вторым**, оно изучает аспекты, которые первое ревью **не охватило**.
> Критические замечания №1–6 из ARCHITECT_REVIEW.md (NanoHTTPD, Foreground Service/WakeLock,
> Path Traversal, SSRF, HTML5 Geolocation, утечка gismeteo_api_key) здесь **не повторяются**.
> Они внесены в plan.md и приняты в работу.

---

## 1. Резюме

После изучения всех файлов проекта выявлено **5 блокирующих проблем** (класс MUST-FIX) и 
**7 значимых рекомендаций** (класс SHOULD-FIX), которые необходимо устранить до начала кодирования
нового функционала. Ниже — полный детальный разбор по 7 запрошенным направлениям.

---

## 2. Направление 1: Модульность и расширяемость `gismeteo_provider.py`

### 2.1. Чистота добавления официального API v2 в существующий класс

**Текущее состояние:**  
[`GismeteoProvider`](file:///home/dmazur/services/weather-informer/gismeteo_provider.py#L47-L74)
имеет конструктор без параметра `api_key`. Класс чётко ограничен в docstring:  
> «Это НЕ официальный публичный API…»

Это правильное разделение. Добавлять `api_key` прямо в существующий `__init__` — значит нарушить
принцип единственной ответственности и смешать два разных протокола в одном классе.

**Проблема 1 (MUST-FIX): Неверный паттерн расширения — нельзя смешивать в одном классе**

ARCHITECTURE_PROPOSAL.md (строки 181–185) и ARCHITECT_REVIEW.md (строки 179–185) предлагают:
```python
# Рекомендация из первого ревью:
def __init__(self, api_key=None, geocode_fn=None, ...):
    self.api_key = (api_key or "").strip()
```
...и затем добавить в `get_weather()` ветку `if self.api_key → вызов v2 REST API`.

Это решение технически работает, но создаёт **внутренний дублирующий граф состояний**:

```
GismeteoProvider._fc_cache  ← кэш XML-прогноза (inf_chrome)
GismeteoProvider.api_key    ← токен v2 REST
GismeteoProvider._v2_quota_blocked  ← (нет в коде — пока не добавлен)
```

При каждом вызове `get_weather()` логика будет:
1. Проверить `self.api_key` → ветка REST v2
2. Если v2 упал → ветка XML (`_get_forecast_for_place()`)  
3. Если XML упал → поднять `GismeteoError` вверх в `caching_server.py`

Внутренний fallback XML живёт внутри `GismeteoProvider`. Внешний fallback (Foreca, Open-Meteo)
живёт в [`_fetch_weather_locked()`](file:///home/dmazur/services/weather-informer/caching_server.py#L1227).
Итого: **два уровня fallback в разных местах, первый скрытый**.

**Рекомендация:** Вместо добавления `api_key` в конструктор существующего класса следует создать
**отдельный модуль** `gismeteo_v2_provider.py`, реализующий тот же интерфейс `get_weather(lat, lon) → dict`
что и `GismeteoProvider`. Это позволит:
- Полностью изолировать REST v2 логику (поиск города по координатам `→ /v2/search/cities/`, 
  запрос прогноза `→ /v2/weather/forecast/aggregate/`, парсинг JSON v2, маппинг иконок)
- Сохранить существующий `GismeteoProvider` (XML) нетронутым, без риска регрессий
- Подключить провайдеры независимо в `caching_server.py`

```
caching_server.py
  ├── GISMETEO_V2 = GismeteoV2Provider(api_key=cfg.get("gismeteo_api_key"))
  └── GISMETEO    = GismeteoProvider(...)  # существующий XML fallback
```

### 2.2. Риски регрессии XML-логики `inf_chrome`

Текущие тесты в [`tests/test_gismeteo.py`](file:///home/dmazur/services/weather-informer/tests/test_gismeteo.py)
полностью мокируют HTTP-слой через `GismeteoProvider._http_get`. При добавлении ветки `api_key` в тот
же класс, тесты 1–11 **не изменятся**, но новая v2-ветка останется без покрытия.

**Проблема 2 (MUST-FIX): Риск регрессии в `_convert()` при правке `_get()` / `_http_get()`.**  
Если `api_key`-ветка меняет `_http_get` (добавляет заголовок `X-Gismeteo-Token`), а `_http_get`
является `@staticmethod` — при монкипатчинге в тестах XML-ветка тоже получит заголовок.
Разделение на два класса или два метода (`_http_get_xml` / `_http_get_v2`) устраняет этот риск.

### 2.3. Корректность каскадного fallback (официальный v2 → inf_chrome → Open-Meteo)

**Проблема 3 (MUST-FIX): Асимметрия обработки quota-блокировки внутри провайдера и снаружи.**

Предложенный в плане флаг `gismeteo_quota_blocked` (аналогично `yandex_quota_blocked` в строке 1258)
должен быть реализован **на уровне `caching_server.py`**, а не внутри `GismeteoProvider`.
Иначе возникает следующий сценарий:

1. Официальный API Gismeteo v2 вернул HTTP 429
2. Если флок внутри провайдера — `caching_server.py` получит `GismeteoError` и перейдёт к Foreca
3. При следующем тике `background_refresher` (через 60 секунд, см. строку 1836) снова попытается
   вызвать `GISMETEO.get_weather()` → провайдер попытается v2 → снова 429 → суточный лимит сгорит!

Нужна **суточная блокировка на уровне `_fetch_weather_locked()`**:
```python
# Аналогично yandex_quota_blocked — переменная уровня модуля с timestamp
gismeteo_v2_quota_blocked_until = 0.0  # unix timestamp до которого заблокировано

def _fetch_weather_locked(...):
    ...
    # Официальный Gismeteo v2 (если задан api_key)
    if want("gismeteo") and cfg.get("gismeteo_api_key") and time.time() > gismeteo_v2_quota_blocked_until:
        try:
            g_data = GISMETEO_V2.get_weather(lat=lat, lon=lon)
            ...
        except GismeteoV2QuotaError:
            # Блокируем до 00:00 UTC следующего дня (минимум 6 часов)
            gismeteo_v2_quota_blocked_until = _next_midnight_utc()
            log.warning("Gismeteo v2 quota exhausted, blocked until %s", ...)
            # Продолжаем в inf_chrome XML ветку
    # inf_chrome XML (без токена)
    if want("gismeteo") and cfg.get("enable_gismeteo_fallback", True):
        ...
```

---

## 3. Направление 2: Интеграция в `caching_server.py`

### 3.1. Добавление `primary_source` (gismeteo как основной) в `get_weather_for()`

**Текущая цепочка в `_fetch_weather_locked()`** (строки 1260–1397):
1. Яндекс (всегда первый)
2. Gismeteo XML (первый резерв)
3. Foreca → Open-Meteo → OWM → 7timer → wttr

Предложение плана: добавить `primary_source: "gismeteo"` и поменять местами Яндекс и Gismeteo.

**Проблема 4 (MUST-FIX): Сломанная логика `yandex_quota_blocked` при `primary_source = "gismeteo"`.**

В строках 1296–1300 текущий код:
```python
probe_interval = cfg.get("fallback_interval_minutes", 15) * 60
fallback_ttl = interval if yandex_quota_blocked else probe_interval
```

Если Gismeteo становится primary, то переменная должна называться `primary_quota_blocked`.
Если Gismeteo дал 429, а Яндекс — рабочий резерв, то `fallback_ttl` должен быть `interval` (60 мин),
а не `probe_interval` (15 мин). **При наивном добавлении блока `if primary_source == "gismeteo"`
перед яндекс-блоком, переменная `yandex_quota_blocked` останется `False` при сбое Gismeteo,
и `fallback_ttl` станет 15 минут — сервер будет долбить резервные источники каждые 15 минут,
вместо того чтобы ждать 60 минут.**

Рекомендация: Рефакторинг `_fetch_weather_locked()` с введением обобщённой переменной 
`primary_quota_blocked` и параметрической цепочки источников, а не дублирования if-блоков.

### 3.2. Race condition в LOCATION_CACHE при новом провайдере

**Существующая защита** реализована корректно через `KEY_LOCKS` (per-key лок) и `LOCATION_CACHE_LOCK`
(глобальный лок на чтение/запись кэша). Анализ кода строк 1163–1177:

```python
with KEY_LOCKS_LOCK:
    lock = KEY_LOCKS.setdefault(key, threading.Lock())
if not lock.acquire(blocking=False):
    # другой поток уже обновляет — отдаём что есть
```

**Проблема 5 (SHOULD-FIX): Добавление нового провайдера GISMETEO_V2 нарушит инвариант `_store_location_result()`.**

В строках 1193–1195:
```python
if data and data.get("src") == "Yandex":
    LOCATION_CACHE[base_key] = {...}
    LOCATION_CACHE[base_key + ":yandex"] = {...}
```

При добавлении v2 API нужно аналогично добавить:
```python
elif data and data.get("src") == "GismeteoV2":
    LOCATION_CACHE[base_key + ":gismeteo_v2"] = {...}
```

Иначе при `sources=(gismeteo,)` фильтре данные от GismeteoV2 и от XML провайдера будут храниться
под одним ключом `base_key + ":gismeteo"`, причём более новый запрос затрёт более старый.  
**Нужны отдельные ключи: `:gismeteo_v2` и `:gismeteo_xml`**.

### 3.3. `gismeteo_quota_blocked` — аналог `yandex_quota_blocked` с суточным кулдауном

Как описано в §3.1, рекомендуется глобальная переменная `gismeteo_v2_quota_blocked_until: float = 0.0`
вместо булевого флага (как у Яндекса). Причина: `yandex_quota_blocked` сбрасывается при каждом тике
`background_refresher` (строка 1258: `yandex_quota_blocked = False`). Для Gismeteo нужно сохранять
состояние **между тиками** (суточный кулдаун), что требует timestamp вместо bool.

```python
# Модульный уровень:
gismeteo_v2_quota_blocked_until: float = 0.0  # 0.0 = не заблокирован

def _next_midnight_utc() -> float:
    """Unix timestamp следующего 00:00:00 UTC (минимум +6 часов)."""
    now = time.time()
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc).timestamp()
    return max(now + 6 * 3600, midnight)
```

---

## 4. Направление 3: Безопасность и конфигурация

### 4.1. Анализ `sanitize_secrets()` — что именно добавить

**Текущий код** [`sanitize_secrets()`](file:///home/dmazur/services/weather-informer/caching_server.py#L179-L197):

```python
# Строка 188:
for k in ("api", "foreca_api_key", "openweathermap_api_key"):
    v = cfg.get(k)
    if v and isinstance(v, str) and len(v.strip()) > 4 and v.strip() != "xxxxxxxx-...-xxxxxxxxxxxx":
        secrets.append(v.strip())
# Строки 194-196:
s = re.sub(r'([?&](?:appid|api_key|apikey|key|token)=)[^&\s]+', r'\1******', ...)
s = re.sub(r'(Bearer\s+)[A-Za-z0-9_\-\.~+/=]+', r'\1******', ...)
s = re.sub(r'(X-Yandex-Weather-Key[:=]\s*)[^\s]+', r'\1******', ...)
```

**Что конкретно добавить** (строка 188 и после строки 196):

```python
# 1. В список ключей конфига (строка 188) добавить "gismeteo_api_key":
for k in ("api", "foreca_api_key", "openweathermap_api_key", "gismeteo_api_key"):

# 2. После строки 196 добавить regex для заголовка X-Gismeteo-Token:
s = re.sub(r'(X-Gismeteo-Token[:=]\s*)[^\s,\'"]+', r'\1******', s, flags=re.IGNORECASE)
```

> [!CAUTION]
> В `config.json` (строка 3) уже есть реальный Яндекс-ключ `api: "0d7e54ed-..."`.
> Строка 190 корректно его маскирует (не равен `xxxxxxxx-...-xxxxxxxxxxxx`).
> Но токен `gismeteo_api_key` ещё не добавлен ни в `DEFAULT_CONFIG` (строки 130–158),
> ни в `config.json` (строки 1–32). Оба файла нужно обновить.

### 4.2. Утечки токена в других местах

**Эндпоинт `/health`** (строки 1560–1581):
```python
"last_error": sanitize_secrets(lem),  # ✅ sanitize применяется
```
После добавления `gismeteo_api_key` в `sanitize_secrets()` этот путь будет защищён.

**Поле `cached_data.get("src")`**: Содержит только строку `"Gismeteo"` или `"GismeteoV2"` — ключ
туда не попадает. ✅ Безопасно.

**Логи через `log.info(...)`**: Во всех местах вызова REST v2 API заголовок `X-Gismeteo-Token`
будет присутствовать в объекте `urllib.request.Request`. Если при ошибке Python напечатает
`repr(req)` или `str(req)` — токен попадёт в лог. Рекомендация: **всегда применять**
`sanitize_secrets()` к строкам исключений перед логированием.

**Дополнительная утечка (SHOULD-FIX): `stats_log_source()` логирует `source`** (строка 566).
Если `source` будет `"GismeteoV2 (token=XXXXX)"` — это утечка. Нужно убедиться, что `src` в 
данных содержит только безопасные строки (`"Gismeteo"`, `"GismeteoV2"`).

**Критическая утечка в `config.json` (MUST-FIX):**  
[`config.json`](file:///home/dmazur/services/weather-informer/config.json) содержит настоящий Foreca JWT-токен
(строка 21) и реальный Яндекс API-ключ (строка 3) в открытом виде в репозитории. Если этот файл
попадёт в git-коммит — секреты будут скомпрометированы. Необходимо добавить `config.json` в 
`.gitignore` и использовать `config.example.json` с заменёнными ключами.

---

## 5. Направление 4: Архитектура кэша и мульти-источники

### 5.1. Как хранятся данные от разных источников

**Текущая схема ключей в LOCATION_CACHE** (анализ строк 1141–1155, 1180–1224):

| Ключ | Значение | Когда создаётся |
|------|----------|-----------------|
| `"56.32:44.0"` (base_key) | Данные основного источника | При любом успешном фетче |
| `"56.32:44.0:yandex"` | Данные Яндекса | Только если `src == "Yandex"` |
| `"56.32:44.0:gismeteo"` | Данные Gismeteo XML | При `sources=("gismeteo",)` |
| `"56.32:44.0:om"` | Данные Open-Meteo | При `sources=("om",)` |

**Проблема (MUST-FIX): Нет разделения ключей `gismeteo_v2` и `gismeteo_xml`.**

При добавлении двух провайдеров Gismeteo (v2 REST и XML `inf_chrome`), оба будут иметь `src == "Gismeteo"`.
Если клиент делает `?source=gismeteo` — непонятно, какой именно кэш отдавать.

**Рекомендация:**
- Официальный REST API v2 → `src = "Gismeteo"`, ключ кэша `":gismeteo"`  
- XML inf_chrome (без токена) → `src = "GismeteoXML"`, ключ кэша `":gismeteo_xml"`  
- HTTP-параметр `?source=gismeteo` → всегда возвращает лучший доступный Gismeteo (v2 если есть)
- HTTP-параметр `?source=gismeteo_xml` → явный выбор XML-провайдера

Обновить `valid_sources` в строке 1785:
```python
valid_sources = ("yandex", "gismeteo", "gismeteo_xml", "foreca", "om", "owm", "7timer", "wttr")
```

### 5.2. Масштабируемость схемы ключей `base_key + ':source'`

Текущий MAX_LOCATIONS = 8 (строка 48). При парке из 5 планшетов с разными городами и включённом
`?source=gismeteo_xml` — каждый планшет может создать 2–3 ключа. 5 × 3 = 15 ключей > MAX_LOCATIONS.

**Проблема (SHOULD-FIX): MAX_LOCATIONS может быть слишком мал при мульти-источниках.**

Рекомендация: Увеличить `MAX_LOCATIONS` до 16 или сделать его конфигурируемым через `config.json`.

---

## 6. Направление 5: Android APK и TLS (ISRG Root X1)

### 6.1. Проблема DST Root CA X3 на Android 6

**Суть проблемы:**  
- Android 6.0 (Marshmallow) поставлялся с системным хранилищем сертификатов, включающим DST Root CA X3
- Этот сертификат **истёк 30 сентября 2021 года**
- Новый корень Let's Encrypt — ISRG Root X1 — отсутствует в системных сертификатах Android 5–7
- Сервер `api.gismeteo.net` использует сертификат, подписанный ISRG Root X1

При попытке Android APK сделать HTTPS-запрос к `api.gismeteo.net` из `OkHttpClient` или
`HttpURLConnection` на Android 6 произойдёт:
```
javax.net.ssl.SSLHandshakeException: 
  java.security.cert.CertPathValidatorException: 
    Trust anchor for certification path not found
```

### 6.2. Реализация: Network Security Config vs. кастомный TrustManager

**Вариант A: Network Security Config (Рекомендуется для API 24+ = Android 7.0+)**

Файл `res/xml/network_security_config.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config>
        <trust-anchors>
            <!-- Системные сертификаты -->
            <certificates src="system" />
            <!-- Добавляем ISRG Root X1 для устройств, где его нет -->
            <certificates src="@raw/isrg_root_x1" />
        </trust-anchors>
    </base-config>
</network-security-config>
```

В `AndroidManifest.xml`:
```xml
<application android:networkSecurityConfig="@xml/network_security_config" ...>
```

Файл `res/raw/isrg_root_x1.pem` — содержит сертификат ISRG Root X1 в PEM формате.

**Ограничение (CRITICAL)**: `networkSecurityConfig` поддерживается с **API 24 (Android 7.0)**.
Целевые планшеты включают Android 6.0 (API 23), где это не работает!

**Вариант B: Кастомный TrustManager (Единственный вариант для Android 5–6)**

```kotlin
// В Kotlin:
fun buildTrustManagerWithISRG(context: Context): X509TrustManager {
    val keyStore = KeyStore.getInstance(KeyStore.getDefaultType())
    keyStore.load(null, null)
    
    // 1. Загружаем все системные сертификаты
    val defaultTmFactory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
    defaultTmFactory.init(null as KeyStore?)
    val defaultTm = defaultTmFactory.trustManagers
        .filterIsInstance<X509TrustManager>().first()
    defaultTm.acceptedIssuers.forEach { keyStore.setCertificateEntry(it.subjectDN.name, it) }
    
    // 2. Добавляем ISRG Root X1 из assets
    val isrgStream = context.assets.open("isrg_root_x1.der")
    val cf = CertificateFactory.getInstance("X.509")
    val isrgCert = cf.generateCertificate(isrgStream)
    keyStore.setCertificateEntry("isrg_root_x1", isrgCert)
    
    // 3. Строим объединённый TrustManager
    val combined = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
    combined.init(keyStore)
    return combined.trustManagers.filterIsInstance<X509TrustManager>().first()
}
```

Затем при создании HTTP-клиента:
```kotlin
val trustManager = buildTrustManagerWithISRG(context)
val sslContext = SSLContext.getInstance("TLS")
sslContext.init(null, arrayOf(trustManager), null)

// OkHttp:
val client = OkHttpClient.Builder()
    .sslSocketFactory(sslContext.socketFactory, trustManager)
    .build()
```

> [!WARNING]
> **Пропуск этой реализации означает**: встроенный `/proxy/weather` на Android 6-планшете
> не сможет сделать HTTPS-запрос к `api.gismeteo.net` через аварийный прямой режим.
> Единственным рабочим путём останется маршрутизация через домашний `caching_server.py`
> (который работает на Linux с актуальными системными сертификатами `/etc/ssl/certs/`).

**Рекомендация:**
1. Для production: использовать **кастомный TrustManager** (работает API 21+)
2. Дополнительно: Network Security Config для API 24+ (более безопасный, декларативный)
3. Настойчиво рекомендовать пользователям работать через домашний `caching_server.py`
   (тогда TLS-рукопожатие делает Linux-сервер с актуальными сертификатами)

### 6.3. Альтернатива: использование Conscrypt

[Conscrypt](https://github.com/google/conscrypt) — Google-библиотека, которая заменяет JCA/JSSE
на современный BoringSSL с актуальными корневыми сертификатами:

```gradle
implementation "org.conscrypt:conscrypt-android:2.5.2"
```

```java
// В Application.onCreate():
Security.insertProviderAt(Conscrypt.newProvider(), 1);
```

Conscrypt автоматически использует собственный бандл доверенных сертификатов, игнорируя
устаревшие системные. **Это самое простое и надёжное решение** для Android 5–7.

---

## 7. Направление 6: Тестирование — что добавить

### 7.1. Покрытие официального API Gismeteo v2

В [`tests/test_gismeteo.py`](file:///home/dmazur/services/weather-informer/tests/test_gismeteo.py) 
тестируется **только** XML-провайдер `inf_chrome`. После создания `GismeteoV2Provider` нужны 
отдельные тесты в `tests/test_gismeteo_v2.py`:

```python
# Обязательные тест-кейсы для GismeteoV2Provider:

# 1. Парсинг JSON v2 /v2/search/cities/ → city_id
def test_parse_cities_json_v2():
    """Правильно извлекает id из JSON-ответа поиска города."""

# 2. Парсинг /v2/weather/forecast/aggregate/ → формат информера
def test_parse_forecast_aggregate():
    """Корректный маппинг: temperature.air.C → fact.temp, давление, ветер."""

# 3. Маппинг иконок v2 (c1_d → skc_d, c3_r2_d → ovc_ra и т.д.)
def test_icon_mapping_v2():
    codes = {
        "c1_d": "skc_d", "c1_n": "skc_n",
        "c2_d": "bkn_d", "c3_d": "ovc",
        "c3_r1_d": "ovc_ra", "c3_s1_d": "ovc_sn",
        "c3_t1_d": "ovc_ts",
    }
    for code, expected in codes.items():
        assert GismeteoV2Provider._map_icon(code) == expected

# 4. Пересчёт давления: mm_hg_atm → pressure_mm (целое)
def test_pressure_conversion():
    """pressure.mm_hg_atm передаётся как float → round() → int."""

# 5. Пересчёт ветра: scale_8 (0..7) → градусы через WD_DEG
def test_wind_direction_scale8():
    WD_DEG = [0, 45, 90, 135, 180, 225, 270, 315]
    for i, deg in enumerate(WD_DEG):
        assert GismeteoV2Provider._wind_deg(i) == deg

# 6. HTTP 401/403 → GismeteoV2QuotaError (не вызывает повтор)
def test_quota_error_on_401_403():
    ...

# 7. HTTP 429 → GismeteoV2QuotaError с суточным кулдауном
def test_quota_error_on_429():
    ...

# 8. Поиск города /v2/search/cities/ → пустой список → GismeteoV2Error
def test_empty_city_search():
    ...

# 9. Сетевой timeout → GismeteoV2Error (временная ошибка, не quota)
def test_network_timeout():
    ...
```

### 7.2. Тесты `sanitize_secrets()` с `gismeteo_api_key`

В [`tests/test_fallback.py`](file:///home/dmazur/services/weather-informer/tests/test_fallback.py)
строки 674–712 уже тестируют `sanitize_secrets()`, но **без `gismeteo_api_key`**.  
Добавить в `test_sanitize_secrets_in_errors_and_status` (строка 674):

```python
def test_sanitize_gismeteo_token(self):
    """gismeteo_api_key и заголовок X-Gismeteo-Token маскируются."""
    secret_gismeteo = "gism-t0k3n-ABCDEF123456"
    cfg = {
        "api": "yandex-key",
        "gismeteo_api_key": secret_gismeteo,
    }
    with patch("caching_server.load_config", return_value=cfg):
        # Значение ключа в теле ошибки
        raw = f"Failed to fetch: token={secret_gismeteo} in url"
        sanitized = caching_server.sanitize_secrets(raw)
        self.assertNotIn(secret_gismeteo, sanitized)
        
        # Заголовок X-Gismeteo-Token
        raw2 = f"Request headers: {{X-Gismeteo-Token: {secret_gismeteo}}}"
        sanitized2 = caching_server.sanitize_secrets(raw2)
        self.assertNotIn(secret_gismeteo, sanitized2)
        self.assertIn("X-Gismeteo-Token: ******", sanitized2)
```

### 7.3. Тесты переключения `primary_source` в `get_weather_for()`

В `tests/test_fallback.py` добавить:

```python
def test_primary_source_gismeteo_called_first(self):
    """При primary_source='gismeteo' провайдер Gismeteo вызывается до Яндекса."""
    calls = []
    
    def fake_gismeteo_v2(**kw): calls.append("gismeteo_v2"); return {"src": "Gismeteo", ...}
    def fake_yandex(*a, **kw): calls.append("yandex"); raise URLError("net")
    
    with patch("caching_server.load_config", return_value={
        "primary_source": "gismeteo",
        "gismeteo_api_key": "test-token",
        ...
    }):
        with patch.object(caching_server.GISMETEO_V2, "get_weather", side_effect=fake_gismeteo_v2):
            res = caching_server._fetch_weather_locked(force=True, lat=56.32, lon=44.0)
    
    self.assertEqual(calls[0], "gismeteo_v2")  # Gismeteo первый
    self.assertNotIn("yandex", calls)           # Яндекс не вызывался
    self.assertEqual(res["src"], "Gismeteo")

def test_primary_source_gismeteo_falls_back_to_yandex(self):
    """При сбое Gismeteo v2 (не quota) переходим на Яндекс, а не на XML."""
    ...

def test_gismeteo_quota_blocked_skips_v2_but_uses_xml(self):
    """При активной quota-блокировке v2 переходит на XML inf_chrome без ошибки."""
    ...
```

### 7.4. Интеграция новых тестов в `run-all.sh`

Добавить в [`tests/run-all.sh`](file:///home/dmazur/services/weather-informer/tests/run-all.sh):
```bash
echo "=== python gismeteo_v2 ===" ; python3 tests/test_gismeteo_v2.py 2>&1 | tail -1
```

---

## 8. Направление 7: `plan.md` — полнота и реализуемость

### 8.1. Неучтённые зависимости между этапами

**Текущая очерёдность в `plan.md`:**
1. Этап 1: Интеграция Gismeteo API (Backend + Client)
2. Этап 2: Веб-сервер на планшете  
3. Этап 3: Zero-ADB развёртывание (APK)
4. Этап 4: Тестирование

**Проблема (MUST-FIX): Этап 1 начинается без необходимого подготовительного этапа.**

Между текущим состоянием кода и Этапом 1 пропущены критические шаги:

```
[Текущий код] → [ЭТАП 0: Подготовка] → [Этап 1] → [Этап 2] → [Этап 3] → [Этап 4]
```

**Этап 0 (пропущен в плане)** должен включать:
1. **Добавление `config.json` в `.gitignore`** (иначе реальные ключи попадут в git)
2. **Создание `config.example.json`** с ключами-заглушками
3. **Расширение `sanitize_secrets()`** (строка 188 + строка 196) до начала работы с новым токеном
4. **Добавление `gismeteo_api_key` в `DEFAULT_CONFIG`** (строки 130–158) и в `config.json`
5. **Добавление `primary_source` в `DEFAULT_CONFIG`**

Эти изменения занимают 30 минут, но без них работа с токеном Gismeteo создаёт уязвимость с первого же коммита.

### 8.2. Реалистичность: Gismeteo API до или после веб-сервера?

**Текущая очерёдность (Этап 1 до Этап 2) верна** из следующих соображений:
- Backend-часть (Python) можно разрабатывать и тестировать без Android-кода
- Провайдер Gismeteo v2 поддаётся unit-тестированию в изоляции (мок HTTP)
- Ошибки в маппинге иконок/давления/ветра можно исправить без перекомпиляции APK

Однако **Этап 3 (APK) жёстко зависит от Этапа 2 (веб-сервер)**:
- APK без готового веб-сервера — просто Kiosk без управления
- Страница `/settings` и `/upload` должны быть спроектированы до создания APK

Обнаружена **неявная зависимость**: APK должен знать формат конфига (`config.json` схему) раньше,
чем будет реализован `/api/settings`. Нужно явно вынести **спецификацию JSON-схемы конфига** в
документ (`config-schema.md` или `ARCHITECTURE_PROPOSAL.md#appendix`) до начала кодирования APK.

### 8.3. Нужен ли рефакторинг перед добавлением новых фич?

**Да, критически нужен** в одном месте:

**`_fetch_weather_locked()`** (строки 1227–1443) — монолитная функция на 216 строк с 7 блоками
try/except. При добавлении Gismeteo v2 она вырастет ещё на 30–40 строк. Это создаёт:
- Сложность отладки (сложно понять, где в цепочке произошёл сбой)
- Риск case-падений при перестановке источников (неправильная логика `yandex_quota_blocked`)

Рекомендуется минимальный рефакторинг:
```python
def _try_yandex(cfg, lat, lon, key, interval):
    """Возвращает (data, quota_blocked: bool) или бросает."""
    ...

def _try_gismeteo_v2(cfg, lat, lon):
    """Возвращает data или бросает (GismeteoV2QuotaError / GismeteoV2Error)."""
    ...

def _try_gismeteo_xml(cfg, lat, lon):
    """Возвращает data или бросает."""
    ...
```

Этот рефакторинг не меняет поведение, но делает `_fetch_weather_locked()` читаемым и тестируемым
в изоляции по каждому источнику.

---

## 9. Матрица критических и значимых замечаний

| № | Критичность | Аспект | Проблема | Файл и строки | Решение |
|---|:-----------:|--------|----------|---------------|---------|
| **1** | 🔴 MUST-FIX | Архитектура | Смешивание v2 REST и XML inf_chrome в одном классе | `gismeteo_provider.py` L47–L74 | Создать `gismeteo_v2_provider.py` |
| **2** | 🔴 MUST-FIX | Архитектура | Отсутствие суточного кулдауна Gismeteo v2 quota на уровне `_fetch_weather_locked()` | `caching_server.py` L1258 | Добавить `gismeteo_v2_quota_blocked_until: float` |
| **3** | 🔴 MUST-FIX | Безопасность | `config.json` с реальными ключами не в `.gitignore` | `config.json` L3, L19, L21 | Добавить в `.gitignore`, создать `config.example.json` |
| **4** | 🔴 MUST-FIX | Безопасность | `gismeteo_api_key` не маскируется в `sanitize_secrets()` | `caching_server.py` L188, L196 | Добавить ключ и regex `X-Gismeteo-Token` |
| **5** | 🔴 MUST-FIX | Архитектура | Пропущен Этап 0 в `plan.md` (подготовительный рефакторинг + безопасность) | `plan.md` L36 | Добавить Этап 0 перед Этапом 1 |
| **6** | 🟡 SHOULD-FIX | Архитектура | Сломанная логика `fallback_ttl` при `primary_source="gismeteo"` | `caching_server.py` L1296–L1300 | Рефакторинг `_fetch_weather_locked()` |
| **7** | 🟡 SHOULD-FIX | Кэш | Коллизия ключей `:gismeteo` для v2 и XML провайдеров | `caching_server.py` L1785, L1193 | Ключи `:gismeteo` и `:gismeteo_xml` |
| **8** | 🟡 SHOULD-FIX | TLS/Android | Отсутствие Conscrypt или кастомного TrustManager в APK-плане | `ARCHITECTURE_PROPOSAL.md` L85–L100 | Добавить секцию TLS в план APK |
| **9** | 🟡 SHOULD-FIX | Тестирование | Нет тестов для GismeteoV2: иконки, давление, ветер, quota | `tests/` | Создать `tests/test_gismeteo_v2.py` |
| **10** | 🟡 SHOULD-FIX | Тестирование | Нет тестов `sanitize_secrets()` для `gismeteo_api_key` и `X-Gismeteo-Token` | `tests/test_fallback.py` L674 | Добавить `test_sanitize_gismeteo_token()` |
| **11** | 🟡 SHOULD-FIX | Тестирование | Нет тестов переключения `primary_source` | `tests/test_fallback.py` | Добавить 3 теста (см. §7.3) |
| **12** | 🔵 NICE-TO-HAVE | Масштабируемость | `MAX_LOCATIONS = 8` может быть мал при мульти-источниках | `caching_server.py` L48 | Увеличить до 16 или сделать конфигурируемым |

---

## 10. Что конкретно изменить в `plan.md` и `ARCHITECTURE_PROPOSAL.md`

### 10.1. Изменения в `plan.md`

**Добавить Этап 0** перед Этапом 1 (после строки 35):

```markdown
### Этап 0: Подготовительный рефакторинг (ОБЯЗАТЕЛЕН ПЕРЕД ЭТАПОМ 1)
1. **Безопасность секретов**:
   - Добавить `config.json` в `.gitignore`; создать `config.example.json` с заглушками
   - В `sanitize_secrets()` (caching_server.py, L188): добавить `"gismeteo_api_key"` в список ключей
   - После L196: добавить `re.sub(r'(X-Gismeteo-Token[:=]\s*)[^\s,\'"]+', r'\1******', s, ...)`
2. **Расширение DEFAULT_CONFIG и config.json**:
   - Добавить `"gismeteo_api_key": ""` и `"primary_source": "yandex"` в DEFAULT_CONFIG (L130–158)
   - Добавить те же ключи в `config.json`
3. **Создание `gismeteo_v2_provider.py`**:
   - Реализовать отдельный класс с интерфейсом `get_weather(lat, lon) → dict`
   - Поддержка `/v2/search/cities/`, `/v2/weather/forecast/aggregate/`
   - Маппинг иконок v2 (c1_d→skc_d и т.д.), давления (mm_hg_atm), ветра (scale_8)
   - Exception `GismeteoV2QuotaError` для 401/403/429
4. **Рефакторинг `_fetch_weather_locked()`**:
   - Декомпозиция на вспомогательные функции `_try_yandex()`, `_try_gismeteo_v2()`, `_try_gismeteo_xml()`
   - Добавить модульную переменную `gismeteo_v2_quota_blocked_until: float = 0.0`
   - Добавить функцию `_next_midnight_utc() → float`
```

**Изменить критерий DoD** (строки 93–101): добавить пункт 6:
```markdown
6. `tests/test_gismeteo_v2.py` содержит тесты JSON-парсера, маппинга иконок, 
   давления и кулдауна квоты; `run-all.sh` включает их прогон.
```

### 10.2. Изменения в `ARCHITECTURE_PROPOSAL.md`

**Раздел 5.2 (строки 181–201)**: Изменить схему модуля провайдера:
- Вместо «добавить `api_key` в конструктор» → «создать отдельный `GismeteoV2Provider`»
- Добавить схему: `GISMETEO_V2 = GismeteoV2Provider(api_key=...)` и `GISMETEO = GismeteoProvider(...)`

**Раздел 3.1 (строки 84–96)**: Добавить подраздел «TLS на Android 6»:
- Описать Conscrypt как рекомендованное решение (1 строка в `build.gradle`, 1 строка в `Application`)
- Указать, что кастомный TrustManager является запасным вариантом

**Раздел 6 (строки 217–235)**: Расширить пункт 1 с добавлением `tests/test_gismeteo_v2.py`
и тестов `sanitize_secrets()` с `gismeteo_api_key`.

---

## 11. Итоговый вердикт

> **CHANGES REQUESTED**

Предложенная архитектура концептуально верна, но содержит **5 блокирующих проблем**,
которые необходимо устранить **до начала кодирования**:

1. **🔴** `config.json` с реальными ключами не в `.gitignore` → немедленная угроза безопасности
2. **🔴** `gismeteo_api_key` не маскируется в `sanitize_secrets()` → токен попадёт в лог при первом же сбое
3. **🔴** Смешивание v2 REST и XML в одном классе → риск регрессии, запутанная логика fallback
4. **🔴** Отсутствие суточного кулдауна Gismeteo v2 quota → квота Gismeteo сгорит за один день
5. **🔴** Пропущен подготовительный Этап 0 в `plan.md` → невозможно безопасно начать Этап 1

После устранения этих 5 проблем и добавления Этапа 0 в план — архитектура готова к реализации
с вердиктом **APPROVED WITH RECOMMENDATIONS**.

Тесты в `tests/` написаны хорошего качества и демонстрируют зрелый подход к тестированию
fallback-цепочки. Их нужно дополнить тестами для новых компонентов (GismeteoV2, sanitize,
primary_source) до начала реализации.
