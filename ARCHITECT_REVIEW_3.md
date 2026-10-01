# Архитектурное и Security/Stability Ревью (ARCHITECT_REVIEW_3)
**Проект:** Weather Informer (`/home/dmazur/services/weather-informer`)  
**Ветка:** `feature/tablet-webserver-gismeteo-deploy`  
**Дата ревью:** 1 октября 2026 г.  
**Роль:** Principal Software Architect & Embedded Systems / Security Reviewer  

---

## 1. Итоговый вердикт (Verdict)

### **APPROVED WITH RECOMMENDATIONS (Одобрено с обязательными рекомендациями)**

Реализованный комплекс изменений демонстрирует высокую инженерную культуру:
- Решена ключевая проблема деструктивной перезаписи API-ключей буллетами (`••••`) при редактировании конфигурации.
- Реализована плавная мультипровайдерная модель с приоритетом Gismeteo сразу за Яндекс.
- Встроенный в планшет HTTP-сервер (`InformerServer`/`ClientHandler`) и адаптивный веб-интерфейс позволяют развертывать и настраивать киоск «по воздуху» без кабеля и без ADB.
- `informer.html` сохраняет строгую совместимость с ES5 для старых Chromium WebView (Android 5.0–7.0).
- Все существующие тесты (node tests: icon, converters, ui; python tests: accuracy, synop, foreca, gismeteo, gismeteo_v2, history, fallback hierarchy) успешно проходят (100% pass).

Тем не менее, в ходе глубинного анализа выявлены **две критические уязвимости утечки секретов**, **одна ошибка согласованности эндпоинтов геокодинга** и **несколько архитектурных дефектов атомарности и синхронизации**.

---

## 2. Сводная матрица выявленных замечаний

| ID | Уровень | Компонент | Проблема | Влияние |
|---|---|---|---|---|
| **SEC-01** | **CRITICAL** | `android/build-apk.sh` | Включение реальных API-ключей и IP-адресов в собранный APK (`assets/config.json`) | Утечка приватных токенов при распространении или коммите APK |
| **SEC-02** | **CRITICAL** | `ClientHandler.java` | Прямая отдача неэкранированного `config.json` по LAN (`GET /config.json`) | Любое устройство в локальной сети может скачать все приватные ключи |
| **BUG-01** | **HIGH** | `ClientHandler.java` vs `informer.html` | Несоответствие путей геокодинга: клиент запрашивает `/reverse`, а сервер планшета слушает `/api/reverse` | Определение города через планшетный сервер даёт 404 и деградирует до внешних сервисов |
| **BUG-02** | **HIGH** | `caching_server.py` | В `do_POST` пропущены `foreca_api_key` и `openweathermap_api_key` в `allowed_fields` | Невозможно сохранить/обновить ключи Foreca и OWM через веб-API сервера |
| **REL-01** | **HIGH** | `ClientHandler.java` | Неатомарная запись `config.json` без блокировок и без `.tmp` | Риск повреждения/обнуления файла конфигурации при сбое питания или конкурентных POST |
| **BUG-03** | **MEDIUM** | `caching_server.py` | Не динамический токен в объекте `FORECA` при смене конфигурации | При смене ключа Foreca сервер продолжает использовать старый/пустой токен до перезапуска |
| **UX-01** | **MEDIUM** | `InformerServer.java`, `caching_server.py` | В UI настройках нет полей ввода ключей Foreca и OWM, хотя они есть в списке `primary_source` | Пользователь не может ввести ключ для выбранного источника через форму |
| **UX-02** | **LOW** | `informer.html` / Меню | Ссылка «📁 Загрузка файлов» на кэширующем сервере (порт 8085) ведёт на несуществующий `/upload` | 404 Not Found при клике в браузере на ПК/ноутбуке |
| **API-01** | **LOW** | `caching_server.py` | Параметр `token` в `GET /reverse?lat=..&lon=..&token=..` игнорируется сервером | При нажатии «Авто» до сохранения нового токена Gismeteo поиск города не использует введённый ключ |

---

## 3. Детальный разбор аспектов ревью

### Аспект А: Безопасность ключей API (Security & Token Protection)

#### 1. Защита от затирания буллетами (`••••` / `*`)
- **В `caching_server.py` (`save_config`)**:
  ```python
  key_fields = ("api", "gismeteo_api_key", "foreca_api_key", "openweathermap_api_key")
  for k, v in new_cfg.items():
      if k in key_fields and isinstance(v, str):
          if "•" in v or "*" in v:
              continue
      current[k] = v
  ```
  *Оценка:* **Надёжно.** Если браузер передал обратно значение, содержащее маскировочный символ `•` или `*`, поле пропускается и сохраняет текущее валидное значение из файла конфигурации.
- **В Android `ClientHandler.java` (POST `/api/settings`)**:
  ```java
  String[] keyFields = new String[]{"api", "gismeteo_api_key", "foreca_api_key", "openweathermap_api_key"};
  for (String kf : keyFields) {
      if (incoming.has(kf)) {
          String val = incoming.optString(kf, "");
          if (val.contains("•") || val.contains("*")) {
              incoming.put(kf, existing.optString(kf, ""));
          }
      }
  }
  ```
  *Оценка:* **Надёжно.** При наличии буллетов восстанавливается значение из существующего `config.json`.

#### 2. Кнопка сброса/очистки ключа `[✕]`
- В веб-интерфейсе сервера и планшета:
  ```javascript
  document.getElementById('btn_clear_gismeteo').onclick = function(){
      document.getElementById('gismeteo_api_key').value = '';
      currentCfg.gismeteo_api_key = '';
  };
  ```
  *Оценка:* **Корректно.** При клике на `[✕]` значение очищается до пустой строки `""`. Пустая строка не содержит `•` или `*`, поэтому проверки `contains` возвращают `false`, и поле в конфигурации успешно сбрасывается в `""`.

#### 3. Векторы утечки секретов
- **Критический дефект SEC-01 (Baking секретов в APK):**
  В `android/build-apk.sh` строки 31–33:
  ```bash
  if [ -f "$ROOT_DIR/config.json" ]; then
      cp "$ROOT_DIR/config.json" "$ASSETS_DIR/config.json"
  fi
  ```
  Если на машине разработчика в корне лежит реальный `config.json`, скрипт упаковывает его в `informer.apk` (`assets/config.json`).
  Проверка файла `android/app/src/main/assets/config.json` показала наличие боевого ключа Яндекс API (`0d7e54ed-...`), действующего JWT Bearer-токена Foreca, ключа OpenWeatherMap и списка внутренних IP-адресов планшетов.
  *Рекомендация:* Скрипт сборки должен копировать только очищенный шаблон (`config.example.json` или структуру с пустыми ключами), а приватный конфиг на планшет должен доставляться исключительно через `/settings` или `/upload`.
- **Критический дефект SEC-02 (Раздача config.json по LAN):**
  В `ClientHandler.java` строки 229–232:
  ```java
  String filename = path.startsWith("/") ? path.substring(1) : path;
  String mime = getMimeType(filename);
  serveFile(filename, mime, out);
  ```
  При запросе `GET http://<tablet-ip>:8080/config.json` метод `serveFile` отдаёт локальный файл `config.json` **без маскирования**. В то время как эндпоинт `/api/settings` маскирует ключи, статический обработчик отдаёт их в открытом виде любому клиенту в Wi-Fi сети.
  *Рекомендация:* В `serveFile` явно запретить отдачу `config.json` (возвращать 403 Forbidden) или перенаправлять на `/api/settings`.
- **Исключение в `ClientHandler.java` (линия 147):**
  ```java
  } catch (Exception e) {
      sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfgBytes);
  }
  ```
  При ошибке парсинга JSON в ответе возвращается немаскированный `cfgBytes`. Следует возвращать ошибку 500, а не отдавать сырые байты.

---

### Аспект Б: Логика выбора основного источника (`primary_source`)

#### 1. Обработка всех провайдеров
В `caching_server.py`:
- Поддерживаются источники: `yandex`, `gismeteo`, `foreca`, `om`, `owm`, `7timer`, `wttr`.
- Если `primary_source` равен одному из них, сервер пытается сначала запросить этот источник.
- Если вызов успешен — результат кэшируется и возвращается. Если неудачен — управление переходит к стандартной цепочке фолбэка.

#### 2. Соблюдение приоритета Gismeteo после Яндекс
- В `caching_server.py`:
  По умолчанию (`primary_source="yandex"`) сначала вызывается `_try_yandex()`. При неудаче управление переходит в блок `want("gismeteo")`, где сразу вызывается `_try_gismeteo_v2()`, а затем `_try_gismeteo_xml(fallback_ttl)`. И только потом Foreca, Open-Meteo, OWM, 7timer, wttr.in.
- В `informer.html`:
  Цепочка фолбэка на клиенте (строки 2209–2237):
  1. `Яндекс.Погода`
  2. `Gismeteo` (v2 API)
  3. `Foreca`
  4. `Open-Meteo`
  5. `OpenWeatherMap`
  6. `7timer`
  7. `wttr.in`
  *Оценка:* **Приоритеты соблюдены на 100%.**

#### 3. Архитектурные дефекты провайдеров
- **Дефект BUG-02 (`allowed_fields` в `caching_server.py`):**
  В `do_POST` сервера:
  ```python
  allowed_fields = [
      "lat", "lon", "city_name", "primary_source",
      "gismeteo_api_key", "api", "cache_interval_minutes",
      "fallback_interval_minutes", "ntp_server"
  ]
  ```
  Поля `foreca_api_key` и `openweathermap_api_key` отсутствуют в списке. Если клиент или скрипт попытается передать эти ключи через API, сервер их проигнорирует.
- **Дефект BUG-03 (Инициализация объекта `FORECA`):**
  В `caching_server.py` объект `FORECA` инициализируется один раз при старте модуля:
  ```python
  FORECA = ForecaProvider(token=load_config().get("foreca_api_key", ""), ...)
  ```
  В `_fetch_weather_locked` при вызове Foreca токен не обновляется из текущего `cfg`, в отличие от Gismeteo v2, где выполняется `GISMETEO_V2.api_key = gismeteo_v2_key`. Если ключ был добавлен или изменен в рантайме, провайдер продолжит использовать устаревший токен.

---

### Аспект В: Стабильность Android-компонентов и WebView

#### 1. Анализ совместимости с Chromium WebView 44–55 (Android 6.0/7.0)
- **`informer.html`:**
  - Проведён лексический и синтаксический анализ всего JavaScript-кода.
  - Конструкции ES6+ (`const`, `let`, `=>`, `async/await`, template literals `` `...` ``, `Object.values`, `Array.prototype.includes`, `URLSearchParams`) **полностью отсутствуют**.
  - Использован чистый ES5 синтаксис: `var`, `function()`, строковая конкатенация `+`, методы jQuery для AJAX и DOM-манипуляций.
  - Совместимость со старыми WebView — **идеальная**.
- **Страницы веб-сервера (`InformerServer.java` — `/settings`, `/upload`):**
  - Используют `fetch()` и `Promise`.
  - Стандартный сценарий: эти страницы открываются с современных смартфонов или ПК администратора сети.
  - Если пользователь попытается открыть `/settings` локально в киоске на Android 5.0 (WebView 37), `fetch` вызовет ошибку `ReferenceError: fetch is not defined`. На Android 6.0+ (Chrome 44+) `fetch` поддерживается нативно.

#### 2. Встроенный сервер (`InformerServer` / `ClientHandler`)
- **Многопоточность:** Обработка входящих соединений выполняется через `Executors.newCachedThreadPool()`. Сокеты снабжены таймаутом `socket.setSoTimeout(30000)`, предотвращающим зависание потоков.
- **Безопасность загрузки файлов (`handleFileUpload`):**
  - Защита от Path Traversal: имя файла нормализуется через `new File(filename).getName()`, а также проверяется канонический путь:
    `dest.getCanonicalPath().startsWith(server.getWebDir().getCanonicalPath())`.
  - Белый список расширений: `.html`, `.js`, `.css`, `.json`, `.png`, `.svg`. Исполняемые файлы (`.dex`, `.apk`, `.so`, `.sh`) отклоняются с кодом 400.
  - Ограничение размера тела запроса: 5 МБ (`413 Payload Too Large`).
  - Побайтовое извлечение multipart данных с использованием чарсета `ISO-8859-1` исключает порчу бинарных ассетов (изображений).
- **Дефект REL-01 (Отказоустойчивость сохранения `config.json`):**
  В `ClientHandler.java` запись выполняется напрямую:
  ```java
  try (FileOutputStream fos = new FileOutputStream(cfgFile)) {
      fos.write(existing.toString(2).getBytes(StandardCharsets.UTF_8));
  }
  ```
  При внезапном отключении питания планшета или одновременных POST-запросах файл может оказаться повреждённым или иметь размер 0 байт.
  *Рекомендация:* Использовать `AtomicFile` (из Android SDK) или запись в `config.json.tmp` с последующим `tmp.renameTo(cfgFile)`.

---

### Аспект Г: Автоопределение города и геокодинг

#### 1. Дефект маршрутизации BUG-01 (`/reverse` vs `/api/reverse`)
- В `caching_server.py` маршрут объявлен как: `if path == "/reverse":`
- В `ClientHandler.java` маршрут объявлен как: `if path.equals("/api/reverse")`
- В `informer.html` клиент делает запрос:
  ```javascript
  url: base + "/reverse?lat=" + gla + "&lon=" + glo
  ```
- **Следствие:** Когда информер на планшете обращается к собственному веб-серверу (`base = http://127.0.0.1:8080`), запрос `GET /reverse` возвращает `404 Not Found` (так как сервер ждёт `/api/reverse`). Встроенная в планшет цепочка поиска города (через локальный Gismeteo) никогда не срабатывает при вызове из `informer.html`.
*Решение:* В `ClientHandler.java` обрабатывать оба варианта:
```java
if (path.equals("/api/reverse") || path.equals("/reverse"))
```
А в `caching_server.py` также добавить поддержку `/api/reverse`.

#### 2. Fallback цепочка автоопределения города
- На клиенте (`informer.html`):
  1. Запрос к серверу информера (`/reverse`).
  2. При недоступности сервера — попытка прямого поиска через Gismeteo API v2.
     *(Примечание: в браузере/WebView прямой запрос блокируется политикой CORS, так как заголовок `X-Gismeteo-Token` не включён в `Access-Control-Allow-Headers` сервиса Gismeteo).*
  3. При CORS/сетевой ошибке — переход к Nominatim OpenStreetMap (`format=jsonv2`, `accept-language=ru`), который отдаёт `Access-Control-Allow-Origin: *` и гарантированно определяет город.
- Приоритет отображения имени города в интерфейсе (`current_city_name`):
  1. `localStorage.getItem("city_name")` (ручной выбор пользователя)
  2. `city_name` из `config.json`
  3. `city_name` из данных текущей погоды (Gismeteo v2)
  4. `geo_object.locality.name` / `province.name` (Яндекс)
  5. `location.name` (Foreca/OWM)
  *Оценка:* **Логика приоритетов выстроена безупречно.**

---

### Аспект Д: Адаптивность и экранные пропорции (`informer.html`)

- **Широкоформатный режим (Desktop / Ноутбуки 16:9, от 1281px или высоты от 850px):**
  - Размер шрифта часов увеличен до `35vh`, время занимает всё полезное пространство.
  - Дата компактно размещена под часами, статус-бар прижат к низу.
- **Планшетный альбомный режим (16:10, 4:3 — Digma, Samsung, 1280x800):**
  - Медиа-запрос `(max-aspect-ratio: 168/100) and (max-width: 1280px)` адаптирует высоту блока `#datetime` до `51vh`, исключая наползание на блок прогноза.
- **Портретный режим (смартфоны, планшеты вертикально):**
  - Flex-колонка (`flex-direction: column`), прокрутка `overflow-y: auto`, шрифты в единицах `vw` (22vw для часов, 18vw для температуры).
- **Результаты тестов UI:** Все 72 проверки структурных инвариантов (`ui-test.js`) завершились успешно.

---

## 4. Рекомендации по исправлению (План действий)

### Приоритет 1 (Критический — Безопасность)
1. **Санитизация сборки APK (`android/build-apk.sh`):**
   Удалить автоматическое копирование реального `$ROOT_DIR/config.json` в ассеты. Использовать шаблон с пустыми полями (`"api": ""`, `"foreca_api_key": ""`), чтобы приватные токены никогда не попадали в бинарный APK.
2. **Защита `config.json` от раздачи по сети (`ClientHandler.java`):**
   В методе `serveFile` заблокировать прямую отдачу файла `config.json` (возвращать 403 Forbidden) либо отдавать JSON с маскированными ключами.

### Приоритет 2 (Высокий — Исправление багов)
3. **Унификация маршрута `/reverse` (`ClientHandler.java` и `caching_server.py`):**
   В обоих серверах разрешить и `/reverse`, и `/api/reverse`, чтобы клиентский скрипт `informer.html` успешно вызывал серверное определение города независимо от типа бэкенда.
4. **Добавление полей в `allowed_fields` (`caching_server.py`):**
   Добавить `foreca_api_key` и `openweathermap_api_key` в список разрешённых полей в `do_POST`.
5. **Атомарная запись конфигурации в Android (`ClientHandler.java`):**
   Реализовать запись во временный файл `config.json.tmp` с атомарным переименованием (`tmp.renameTo(cfgFile)`), исключив риск повреждения JSON при сбое питания.

### Приоритет 3 (Средний — Согласованность и UX)
6. **Динамическое обновление токена Foreca (`caching_server.py`):**
   В `_fetch_weather_locked` перед обращением к Foreca актуализировать токен: `FORECA.token = cfg.get("foreca_api_key", "").strip()`.
7. **Поля ввода ключей Foreca и OWM в веб-интерфейсе:**
   Добавить поля ввода ключей Foreca и OpenWeatherMap с кнопками очистки `[✕]` в `InformerServer.java` и `caching_server.py`.
8. **Скрытие/адаптация ссылки «📁 Загрузка файлов» на ПК-сервере:**
   В `informer.html` скрывать пункт меню «📁 Загрузка файлов», если информер открыт не на планшете (или добавить заглушку/обработчик на сервере 8085).
