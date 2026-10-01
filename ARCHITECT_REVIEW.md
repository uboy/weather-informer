# Архитектурное ревью (Architectural Review)
## Модернизация Weather Informer: Встроенный сервер на планшете, Zero-ADB деплой и официальный API Gismeteo

**Дата ревью:** 01 октября 2026 г.  
**Проект:** `/home/dmazur/services/weather-informer`  
**Ревьюер:** Principal Systems Architect & Systems Reviewer  
**Статус:** **APPROVED WITH RECOMMENDATIONS (Одобрено с обязательными рекомендациями)**  

---

## 1. Резюме и общий вердикт

Представленный комплекс предложений ([ARCHITECTURE_PROPOSAL.md](file:///home/dmazur/services/weather-informer/ARCHITECTURE_PROPOSAL.md), [research.md](file:///home/dmazur/services/weather-informer/research.md), [plan.md](file:///home/dmazur/services/weather-informer/plan.md)) нацелен на устранение фундаментальных операционных болей проекта:
1. Избавление от ограничений контекста `file:///` и хрупкой зависимости от root-прав для инъекции настроек WebView Kiosk.
2. Кардинальное упрощение онбординга новых планшетов (переход от связки «ПК + Linux + ADB + root + консольные bash-скрипты» к сценарию управления со смартфона).
3. Интеграция официального токена Gismeteo с сохранением жесткой защиты суточных лимитов и каскадным отказоустойчивым резервированием.

Концептуальное направление выбрано абсолютно верно: перенос рантайма информера с `file://` на локальный протокол `http://127.0.0.1:8080` решает проблемы CORS между локальными файлами, изолированности `localStorage` и открывает возможность горячего обновления интерфейса без физического доступа к устройству.

Тем не менее, в предложенной архитектуре выявлен ряд **критических платформенных ловушек (Android 6.0/7.0, 1 ГБ RAM), уязвимостей безопасности и нереализуемых предположений**, которые приведут к сбоям или компрометации системы при наивной реализации.

### Итоговый вердикт:
> **APPROVED WITH RECOMMENDATIONS**  
> Архитектура принимается к реализации **при условии обязательного устранения 5 критических замечаний**, детально разобранных ниже.

---

## 2. Глубокий анализ по ключевым направлениям

### Направление А: Веб-сервер на планшете (Android 6.0–7.0, 1 ГБ RAM)

#### 1. Платформенная совместимость и жизненный цикл (RAM / LMK / Doze)
* **Фатальная ошибка в выборе стека**: В предложении ([ARCHITECTURE_PROPOSAL.md#L85](file:///home/dmazur/services/weather-informer/ARCHITECTURE_PROPOSAL.md#L85)) упомянут `com.sun.net.httpserver.HttpServer`.  
  **Реальность Android**: Пакет `com.sun.net.httpserver.*` **отсутствует** в Android SDK (как в Android 6.0 Marshmallow, так и во всех последующих версиях Android runtime/ART). Попытка его использования приведет к ошибкам сборки или `NoClassDefFoundError` в рантайме.  
  *Рекомендация*: Использовать исключительно **NanoHTTPD** (версия 2.3.1) либо компактный самописный `ServerSocket` на 100 строк кода без внешних библиотек.
* **Поведение LowMemoryKiller (LMK) на 1 ГБ RAM**:
  На планшетах Digma/Oysters/Irbis с 1 ГБ RAM ОС отдает пользовательским приложениям не более 300–400 МБ. Сам системный WebView (Chromium) при непрерывной отрисовке часов и анимаций съедает 150–220 МБ.
  Если запускать сервер внутри одного процесса с Activity без Foreground Service:
  - При нехватке памяти LMK прибьет процесс целиком.
  - Попытка зайти на `http://<IP_планшета>:8080/settings` со смартфона вернет `ERR_CONNECTION_REFUSED`.
  *Рекомендация*: Веб-сервер обязан оформляться как **Foreground Service** с постоянным низкоприоритетным уведомлением (`startForeground()`) и приоритетом `oom_score_adj <= 0`, либо запускаться в отдельном изолированном легковесном процессе (`android:process=":server"`), который потребляет всего 8–12 МБ RAM и никогда не падает при крашах тяжелого рендерера Chromium.
* **DuraSpeed и Doze Mode на чипсетах MediaTek**:
  Бюджетные планшеты на MT8321/MT6580 содержат проприетарный механизм DuraSpeed, который через 10–15 минут после выключения экрана или в фоновом режиме намертво замораживает сокеты и отключает Wi-Fi.
  *Рекомендация*: В манифесте и рантайме обязательны:
  - `PowerManager.PARTIAL_WAKE_LOCK` (удержание CPU при работе сервера);
  - `WifiManager.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "WeatherServerWifiLock")`;
  - Запрос исключения из оптимизации батареи (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`).

#### 2. Безопасность локального веб-сервера и загрузки файлов
* **Критическая уязвимость 1: Path Traversal в `/api/upload`**:
  Предложение предусматривает загрузку файлов (`informer.html`, скрипты). Если имя файла из multipart/form-data принимается как есть:
  `filename = part.getSubmittedFileName()` -> `File target = new File(baseDir, filename);`
  Атакующий в локальной сети сможет передать `../../../../data/data/com.informer/shared_prefs/config.xml` или переписать системные файлы приложения.
  *Рекомендация*:
  1. Извлекать строго `new File(filename).getName()`.
  2. Проверять канонический путь: `targetFile.getCanonicalPath().startsWith(uploadDir.getCanonicalPath())`.
  3. Ввести **строгий белый список разрешенных файлов**: `informer.html`, `config.json`, `config.js`, `jquery.min.js`, `style.css`, иконки `.png`/`.svg`. Никаких `.dex`, `.so`, `.sh` или исполняемых скриптов!
* **Критическая уязвимость 2: Отсутствие аутентификации и CSRF в домашней сети**:
  Сервер слушает на `0.0.0.0:8080`. Любое устройство в Wi-Fi (смарт-ТВ, гостевой телефон, скомпрометированная умная розетка) или даже сторонний сайт, открытый в браузере любого домашнего ПК (через простой `fetch('http://192.168.1.42:8080/api/settings', {method: 'POST', body: ...})` без preflight для простых запросов), сможет перезаписать настройки, похитить API-токены или исказить отображаемые данные.
  *Рекомендация*:
  - Реализовать простой **Setup PIN** (4 цифры, по умолчанию отображается на экране информера в углу).
  - Проверять заголовок `Origin` / `Referer` при POST-запросах, запрещая межсайтовые вызовы.
  - Маскировать токены при отдаче `GET /api/settings`.
* **Критическая уязвимость 3: Open SSRF в `/proxy/weather`**:
  Эндпоинт `/proxy/weather` не должен быть слепым HTTP-прокси. Если он принимает произвольный параметр `?url=...`, злоумышленник может использовать планшет для сканирования внутренней подсети и атак на роутер (`http://192.168.1.1`).
  *Рекомендация*: Прокси должен принимать только параметр `provider` (`gismeteo`, `yandex`, `openmeteo`), формируя целевой URL строго из жестко зашитого в коде белого списка доменов (`api.gismeteo.net`, `api.weather.yandex.ru`, `api.open-meteo.com`).

---

### Направление Б: Сценарий Zero-ADB деплоя и обратная совместимость

#### 1. Реалистичность создания единого APK («Kiosk + Server»)
В текущем репозитории [weather-informer](file:///home/dmazur/services/weather-informer) **полностью отсутствует** Android Gradle проект (есть только Python-сервер, HTML/JS и bash-скрипты под существующий `uk.nktnet.webviewkiosk`).  
Создание собственного APK с нуля требует:
- Настройки Android SDK / Gradle wrapper / сборки CI;
- Реализации Kiosk-логики: перехват кнопок Home/Back, полноэкранный режим `SYSTEM_UI_FLAG_IMMERSIVE_STICKY`, удержание экрана `FLAG_KEEP_SCREEN_ON`, обработка перезагрузки `RECEIVE_BOOT_COMPLETED`;
- Поддержки Geolocation permissions для WebView.

*Оценка трудоемкости*: Это отдельный подпроект (300–500 строк Java/Kotlin кода).  
*Рекомендация*: Сборка собственного APK — правильный шаг, но его исходный код должен быть вынесен в подкаталог `android/` с автономным `build.gradle` и скриптом сборки через `./gradlew assembleRelease` (или headless `docker-android`).

#### 2. Платформенные ловушки развёртывания со смартфона
1. **Блокировка «Неизвестных источников» (Unknown Sources)**:  
   Планшеты на Android 6.0/7.0 из коробки блокируют установку сторонних APK. Пользователю **в любом случае** придется один раз зайти в `Настройки -> Безопасность -> Неизвестные источники` и включить галочку. Этот шаг обязан присутствовать в документации для пользователя.
2. **Ловушка HTML5 Geolocation на незащищенном HTTP**:  
   В [ARCHITECTURE_PROPOSAL.md#L106-L107](file:///home/dmazur/services/weather-informer/ARCHITECTURE_PROPOSAL.md#L106-L107) указано:
   > *«Кнопка «Определить координаты текущего устройства» (использует HTML5 Geolocation смартфона, с которого открыта страница)»*  
   **КРИТИЧЕСКИЙ БАГ СОВРЕМЕННЫХ БРАУЗЕРОВ**: Все современные мобильные браузеры (Chrome на Android, Safari на iOS) **намертво блокируют** `navigator.geolocation` на небезопасных источниках (non-HTTPS IP-адресах вроде `http://192.168.1.42:8080/settings`)! Вызов `navigator.geolocation.getCurrentPosition()` вернет ошибку или будет `undefined`.  
   *Рекомендация*: Не полагаться на HTML5 Geolocation смартфона по HTTP. Реализовать поиск города по названию через локальный бэкенд Nominatim (`/geocode?q=...`), как уже сделано в [caching_server.py#L1583-L1615](file:///home/dmazur/services/weather-informer/caching_server.py#L1583-L1615).

#### 3. Совместимость со старыми WebView (Chromium 44–53 на Android 6.0)
* **Ограничения JavaScript**:
  Заводской WebView Android 6.0 базируется на **Chromium 44**.  
  - Нет поддержки ES6 Arrow Functions, `async/await`, `Promise.finally`, Object rest/spread `...`.
  - Метод `fetch()` поддерживается не полностью.
  - Любой JS-код в [informer.html](file:///home/dmazur/services/weather-informer/informer.html) и в новой странице `/settings` **обязан оставаться строго в стандарте ES5** (функции `function()`, `var`, колбэки jQuery или XMLHttpRequest). Использование современного ES6+ приведет к белому экрану на планшете.
* **Проблема устаревших корневых сертификатов Let's Encrypt (DST Root CA X3)**:
  В сентябре 2021 года истек корневой сертификат DST Root CA X3. На Android 6.0/7.0 отсутствует встроенный новый корень `ISRG Root X1`.  
  Если планшет попытается сделать прямой HTTPS-запрос к внешнему API (`https://api.gismeteo.net` или `https://api.weather.yandex.ru`), он выбросит **`SSLHandshakeException: CertPathValidatorException: Trust anchor for certification path not found`**!  
  *Рекомендация*: Встроенный в APK HTTP-клиент для `/proxy/weather` обязан либо иметь кастомный `TrustManager` с встроенным сертификатом ISRG Root X1, либо использовать движок `Conscrypt`, либо основной трафик всегда должен идти через домашний `caching_server.py` (Python в Linux использует свежие системные сертификаты `/etc/ssl/certs/ca-certificates.crt`).

---

### Направление В: Интеграция официального API Gismeteo

#### 1. Квоты, Rate Limiting и утечки токенов
* **Критический дефект в защите секретов**:  
  В текущем файле [caching_server.py#L188](file:///home/dmazur/services/weather-informer/caching_server.py#L188) функция `sanitize_secrets()` очищает только:
  ```python
  for k in ("api", "foreca_api_key", "openweathermap_api_key"):
  ```
  И заголовки: `X-Yandex-Weather-Key`, `Bearer`.  
  **Токен `gismeteo_api_key` и заголовок `X-Gismeteo-Token` не маскируются!**  
  При первой же ошибке запроса к Gismeteo токен пользователя попадет в открытом виде в `server.log`, а также вернется в JSON-ответе эндпоинта `/health` и `/status` в поле `last_error`!  
  *Рекомендация*: Немедленно добавить `gismeteo_api_key` и `X-Gismeteo-Token` в `sanitize_secrets()`.

* **Опасность прямого режима нескольких планшетов**:  
  Если у пользователя 5 планшетов, и они настроены на «прямое подключение» к Gismeteo через локальный прокси:
  5 планшетов × 24 запроса/сутки = **120 запросов в сутки**.  
  Если тариф Gismeteo имеет суточную квоту 50 или 100 запросов, лимит исчерпается уже к середине дня.  
  *Рекомендация*: Прямое подключение к Gismeteo с планшета должно быть **строго аварийным** (Fallback при падении `caching_server.py`) с увеличенным интервалом опроса (например, 90–120 минут для клиента). По умолчанию все планшеты должны брать данные с домашнего `caching_server.py`.

#### 2. Архитектура Fallback и Rate Limit Cooldown
* При возврате HTTP 429 (Too Many Requests) или HTTP 403 (Quota Exceeded / Invalid Token):
  - Сервер не должен повторять попытки каждые 15 минут (`fallback_interval_minutes`).
  - Должен выставляться флаг блокировки квоты `gismeteo_quota_blocked = True` с кулдауном до конца текущих суток (00:00 UTC) либо минимум на 6 часов.
  - Каскад переключения:  
    `Официальный API v2 (api.gismeteo.net)` -> `XML inform-service (inf_chrome)` -> `Foreca` -> `Open-Meteo`.

#### 3. Маппинг параметров Gismeteo v2 JSON -> Informer Model
Структура официального v2 JSON отличается от внутреннего XML `inf_chrome`:
- **Давление**: Официальный API отдает `pressure.mm_hg_atm` (число) и `pressure.h_pa`. Информер ожидает целочисленное `pressure_mm`.
- **Ветер**: Скорость в `wind.speed.m_s`, румб в `wind.direction.scale_8` (шкала 0..7) или градусы. В информере требуется пересчет в градусы `WD_DEG = [0, 45, 90, 135, 180, 225, 270, 315]`.
- **Иконки**: Коды погоды Gismeteo v2 (например, `c1_d`, `c3_r2_d`) должны транслироваться в канонические классы информера:
  - Ясно: `skc_d` / `skc_n`
  - Малооблачно / Переменная облачность: `bkn_d` / `bkn_n`
  - Пасмурно: `ovc`
  - Дождь: `ovc_ra`
  - Снег: `ovc_sn`
  - Гроза: `ovc_ts`
- **Агрегированный прогноз (Parts of Day)**:
  Эндпоинт `/v2/weather/forecast/aggregate/` отдает части суток (`night`, `morning`, `day`, `evening`). Их необходимо упаковать в структуру `forecasts[0].parts`, совместимую с версткой правого блока [informer.html](file:///home/dmazur/services/weather-informer/informer.html).

---

## 3. Матрица ловушек и граничных случаев (Edge Cases Matrix)

| № | Граничный случай / Ловушка | Последствия без доработки | Архитектурное решение |
|---|---|---|---|
| 1 | `com.sun.net.httpserver` в Android | Сборка не соберется или креш `ClassNotFoundException` | Использовать `NanoHTTPD` 2.3.1 (1 JAR / исходник 1 файл). |
| 2 | HTML5 Geolocation со смартфона по HTTP | Браузер смартфона молча заблокирует геолокацию | Использовать поиск города по строке через серверный `/geocode`. |
| 3 | Устаревший SSL-сертификат DST Root CA X3 на Android 6 | Ошибка TLS `Trust anchor not found` при прямом вызове Gismeteo | Встроить ISRG Root X1 в TrustManager приложения или роутить через домашний сервер. |
| 4 | Атака Path Traversal через `/api/upload` | Перезапись приватных файлов приложения или системных баз | Санитизация `getName()`, валидация пути `getCanonicalPath()` и белый список расширений. |
| 5 | Open Proxy / SSRF на порту 8080 | Сканирование и эксплуатация домашней сети | Белый список целевых хостов (Gismeteo, Yandex, Open-Meteo). |
| 6 | Утечка `gismeteo_api_key` в логи и `/health` | Компрометация платного токена | Добавить токен и заголовок `X-Gismeteo-Token` в `sanitize_secrets()`. |
| 7 | Засыпание Wi-Fi на Android 6 (DuraSpeed) | Веб-сервер недоступен по сети через 15 мин после блокировки | Взять `WifiLock(WIFI_MODE_FULL_HIGH_PERF)` и `WakeLock`. |
| 8 | Вытеснение из RAM (LMK) процесса WebView | Сервер падает вместе с вкладкой браузера | Вынести сервер в `Foreground Service` с низким `oom_score_adj`. |
| 9 | Расход суточной квоты при нескольких планшетах | Блокировка токена Gismeteo к середине дня | Все планшеты по умолчанию тянут кэш с `caching_server.py`. |

---

## 4. Конкретные рекомендации по внесению изменений в план и код

### 1. Исправления в `caching_server.py`:
- Расширить `sanitize_secrets()`:
  ```python
  for k in ("api", "foreca_api_key", "openweathermap_api_key", "gismeteo_api_key"):
      ...
  s = re.sub(r'(X-Gismeteo-Token[:=]\s*)[^\s]+', r'\1******', s, flags=re.IGNORECASE)
  ```
- В `config.json` и `DEFAULT_CONFIG` добавить:
  ```python
  "gismeteo_api_key": "",
  "primary_source": "yandex",  # "yandex" или "gismeteo"
  ```
- В `get_weather_for()` реализовать логику выбора первичного источника: если `primary_source == "gismeteo"`, сначала вызывать Gismeteo, а Яндекс использовать как резерв.

### 2. Доработки в `gismeteo_provider.py`:
- Добавить конструктор с поддержкой `api_key`:
  ```python
  def __init__(self, api_key=None, geocode_fn=None, city_cache_path=None, logger=None):
      self.api_key = (api_key or "").strip()
      ...
  ```
- В метод `get_weather()`: если `self.api_key` задан — вызывать официальный REST API `https://api.gismeteo.net/v2/` с заголовком `X-Gismeteo-Token`. При ошибках 401/403/429 логировать предупреждение и бесшовно переходить на внутренний XML `inf_chrome`.

### 3. Требования к реализации Android APK («Informer Kiosk & Server»):
- **Стек**: Java/Kotlin, `minSdkVersion 21` (Android 5.0), `targetSdkVersion 28` (Android 9.0 — оптимально для старых устройств без жестких ограничений scoped storage).
- **HTTP-сервер**: Встроенный `NanoHTTPD` в рамках Foreground Service.
- **Безопасность `/api/upload`**:
  - Каталог назначения: строго приватный `context.getFilesDir() + "/www/"`.
  - Запрет перезаписи файлов конфигурации без PIN-кода.
  - Лимит размера входящего файла: 5 МБ.
- **Kiosk WebView**:
  - `setWebViewClient` с перехватом `onReceivedError` (показ красивой заглушки вместо стандартного «Веб-страница недоступна»).
  - Включение `getSettings().setDomStorageEnabled(true)` и `getSettings().setJavaScriptEnabled(true)`.
  - Удержание экрана: `getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)`.

---

## 5. Итоговое заключение

Предложенное архитектурное решение является **зрелым, логичным и своевременным эволюционным шагом** для проекта Weather Informer. Переход на встроенный веб-сервер и официальный API Gismeteo решит застарелые инфраструктурные проблемы с CORS, root-правами и жесткими лимитами Яндекса.

При соблюдении указанных выше рекомендаций по безопасности (санитизация upload, маскирование токена, защита от CSRF/SSRF) и платформенных ограничений Android 6 (NanoHTTPD, ES5-совместимость, Foreground Service, WakeLock) система обеспечит непрерывную автономную работу погодных информеров на парке старых планшетов.

**План к реализации рекомендуется утвердить с внесенными корректировками.**
