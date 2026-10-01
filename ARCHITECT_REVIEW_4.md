# Архитектурное и Security/Regression Ревью (ARCHITECT_REVIEW_4)
**Проект:** Weather Informer (`/home/dmazur/services/weather-informer`)  
**Ветка:** `feature/tablet-webserver-gismeteo-deploy`  
**Дата ревью:** 1 октября 2026 г.  
**Роль:** Principal Software Architect & Embedded Systems / Security Reviewer  

---

## 1. Итоговый вердикт (Verdict)

### **CHANGES REQUESTED (Требуются обязательные исправления)**

Внедренные доработки успешно решают большинство замечаний из предыдущего ревью (`ARCHITECT_REVIEW_3`):
- `tests/run-all.sh` полностью успешен (100% pass: node tests, python tests).
- Обеспечена изоляция боевых ключей от APK-артефакта в `android/build-apk.sh`.
- Введена защита от прямого скачивания `config.json` по LAN.
- Добавлен расчет ветра для завтрашних слотов в Open-Meteo (`partWind(h0, h1, tomorrowStrOm)` и `tmrwWind`).
- Синхронизированы алиасы `/reverse` и `/api/reverse`.
- Добавлены кнопки сброса ключей `[✕]` и маскирование в UI и API.
- Код фронтенда сохраняет строгую ES5-совместимость для старых Chromium WebView (Android 5.0–7.0).

Тем не менее, в процессе глубинного анализа кодовой базы выявлены **два критических функционально-архитектурных дефекта**, приводящих к исчерпанию суточных лимитов внешних API и неработоспособности динамического обновления токенов, а также **две уязвимости обхода защиты конфигурации**.

---

## 2. Сводная матрица выявленных замечаний

| ID | Уровень | Компонент | Проблема | Влияние |
|---|---|---|---|---|
| **BUG-01** | **CRITICAL** | `informer.html` / `MainActivity.java` | Параметр `?force=1` не сбрасывается и не поглощается после первого вызова `run()`. Проверка `isForce` срабатывает **каждую минуту**. | **Исчерпание суточных лимитов API** (Яндекс/Gismeteo 50-100 запросов сгорают за ~1 час), постоянный флуд сервера. |
| **BUG-02** | **CRITICAL** | `caching_server.py` vs `foreca_provider.py` | В `caching_server.py` присваивается `FORECA.token = ...`, но `ForecaProvider` хранит токен в `self._token` и не имеет сеттера `token`. | **Токен Foreca на сервере не обновляется**. Запросы продолжают идти со старым/пустым токеном, вызывая 401 Unauthorized. |
| **SEC-01** | **HIGH** | `ClientHandler.java` | Блок `catch (Exception e)` в `POST /api/settings` напрямую перезаписывает `config.json` телом запроса `fos.write(body)`. | При невалидном JSON или ошибке парсинга файл конфигурации повреждается или затирается маскированными буллетами. |
| **SEC-02** | **HIGH** | `ClientHandler.java` | Эндпоинт загрузки файлов `/api/upload` разрешает расширение `.json` и не запрещает файл `config.json`. | Любой клиент из локальной сети может перезаписать `config.json` в обход `/api/settings` и проверок маскирования. |
| **SEC-03** | **MEDIUM** | `ClientHandler.java` | Запрет прямого доступа к файлу проверяет только `filename.equals("config.json")`, но не `config.json.tmp`. | При наличии временного файла после прерванной записи его можно скачать без маскирования через `GET /config.json.tmp`. |
| **UX-01** | **MEDIUM** | `InformerServer.java`, `caching_server.py` | В выпадающий список `primary_source` добавлены `foreca` и `owm`, но поля ввода для `foreca_api_key` и `openweathermap_api_key` в формах настроек отсутствуют. | Пользователь, выбравший Foreca или OWM, не имеет возможности ввести API-ключи через веб-интерфейс. |

---

## 3. Детальный технический разбор изменений

### 3.1. `informer.html` и `android/app/src/main/assets/informer.html`

Файлы `informer.html` и `android/app/src/main/assets/informer.html` проверены на идентичность (`diff -u` возвращает 0 — файлы побайтово идентичны).

#### а) Расчет скорости и направления ветра для завтрашних слотов Open-Meteo
- Реализована функция `tmrwWind(h0, h1)`, передающая `tomorrowStrOm` в параметризованный `partWind(h0, h1, targetDay)`.
- Для углов ветра корректно используется векторное суммирование углов (`Math.atan2(sinSum / da, cosSum / da)`), что математически корректно для направлений (круговая статистика).
- Скорость ветра переводится из км/ч в м/с (`ws[wi] / 3.6`) и округляется до 1 знака после запятой.
- Слоты завтрашней ночи `[0..6)` и утра `[6..12)` получают валидные значения `wind_speed` и `wind_angle`.

#### б) Поддержка `primary_source_cfg` из `config.json`
- В `load_config` считывается поле `cfg.primary_source`.
- В `render_src` и `get_data` приоритет выбора источника скорректирован: если в `localStorage` не задан выбор (`!pref || pref === "default"`), по умолчанию активируется `primary_source_cfg || "default"`.
- Список поддерживаемых провайдеров (`server`, `yandex`, `gismeteo`, `foreca`, `om`, `owm`, `7timer`, `wttr`) полностью согласован.

#### в) Параметр `?force=1` и проблема бесконечного опроса (CRITICAL BUG-01)
В коде функции `run()`:
```javascript
var isForce = (window.location && window.location.search && window.location.search.indexOf("force=1") !== -1);
if ((isForce || lastcall < now - _effTimeout) && (server_url.trim() !== '' || api != 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')) {
    get_data();
}
```
**Суть дефекта:**
`run()` вызывается каждую минуту по таймеру (`setTimeout(function(){run()}, timer)`). Когда `MainActivity.java` загружает URL `http://127.0.0.1:8080/?force=1`, строка `window.location.search` остается равной `"?force=1"` на протяжении всего жизненного цикла страницы.
Следовательно:
1. На 0-й секунде срабатывает `get_data()`. Значение `lastcall` обновляется.
2. Через 60 секунд снова вызывается `run()`. `lastcall < now - _effTimeout` равно `false` (прошла лишь 1 минута из 15–60 минут интервала кэширования).
3. **Однако `isForce` по-прежнему `true`!**
4. В результате `get_data()` вызывается **каждую минуту**, 60 раз в час, 1440 раз в сутки!
5. Если настроен прямой Яндекс или Gismeteo, суточный бесплатный лимит (30–50 запросов) выгорает менее чем за час работы планшета, и информер блокируется сервером провайдера (HTTP 429 / 403).

**Решение:** Флаг форсирования должен поглощаться при первом срабатывании, а URL должен очищаться через `history.replaceState`:
```javascript
var force_consumed = false;
...
var isForce = false;
if (!force_consumed && window.location && window.location.search && window.location.search.indexOf("force=1") !== -1) {
    isForce = true;
    force_consumed = true;
    if (window.history && window.history.replaceState) {
        try {
            window.history.replaceState({}, document.title, window.location.pathname);
        } catch (e) {}
    }
}
```

#### г) Строгая ES5-совместимость для WebView Android 6.0
- Проведен статический AST- и regex-анализ всех `<script>` тегов в `informer.html` (93 465 символов скриптов).
- В скриптах **полностью отсутствуют**:
  - `let` и `const` (все переменные объявлены через `var`);
  - стрелочные функции `=>`;
  - шаблонные строки с обратными кавычками ` `...` `;
  - ключевые слова `async` и `await`.
- Промисы (`fetch().then().catch()`) на Android 6.0 (Chromium 44+) поддерживаются нативно.

---

### 3.2. `android/app/src/main/java/ru/weather/informer/MainActivity.java`

- Вызов `webView.loadUrl("http://127.0.0.1:8080/?force=1")` корректно инициирует форсированную перезагрузку при старте и вызове `reloadInformer()`.
- Кэш браузера перед этим очищается (`webView.clearCache(true)`).
- В сочетании с исправлением поглощения флага в `informer.html` (см. BUG-01) механизм работает надежно.

---

### 3.3. `android/app/src/main/java/ru/weather/informer/ClientHandler.java`

#### а) Атомарное сохранение `config.json.tmp -> config.json`
- Запись во временный файл `config.json.tmp` с последующим вызовом `tmpFile.renameTo(cfgFile)` предотвращает чтение полузаписанного JSON.
- Имеется фолбэк с повторной попыткой и удалением `tmpFile`.

#### б) Защита сырого `config.json` от внешнего доступа (403 Forbidden)
- В `serveFile` добавлена проверка адреса клиента:
```java
if (filename.equals("config.json")) {
    InetAddress addr = socket.getInetAddress();
    boolean isLocal = addr != null && (addr.isLoopbackAddress() || "127.0.0.1".equals(addr.getHostAddress()));
    if (!isLocal) {
        sendResponse(out, 403, "Forbidden", "text/plain", ...);
        return;
    }
}
```
- **Замечание (SEC-03):** Проверка строго на `filename.equals("config.json")`. Если запросить `/config.json.tmp`, файл отдается без проверки. Рекомендуется проверять `filename.startsWith("config.json")`.

#### в) Опасный блок `catch` при сохранении настроек (SEC-01)
В строках 181–185:
```java
} catch (Exception e) {
    try (FileOutputStream fos = new FileOutputStream(cfgFile)) {
        fos.write(body);
    }
}
```
Если клиент отправил некорректный JSON или произошел сбой парсинга, блок `catch` записывает сырое тело `body` прямо в `config.json`. Это может затереть ключи маскированными буллетами или повредить JSON.
**Решение:** В блоке `catch` возвращать HTTP 400/500, не трогая существующий `config.json`.

#### г) Уязвимость в эндпоинте `/api/upload` (SEC-02)
В `handleFileUpload`:
- Разрешено расширение `.json` (`!lower.endsWith(".json") ...`).
- Имя файла `filename = new File(filename).getName();`
- Файл сохраняется в `server.getWebDir()`.
Если клиент из LAN отправляет POST `/api/upload?file=config.json`, сервер перезапишет рабочий `config.json` произвольным содержимым без какой-либо аутентификации и без сохранения существующих ключей.
**Решение:** Явно запретить загрузку файлов с именем `config.json` или `config*.json` через `/api/upload`.

#### д) Алиасы `/reverse` и `/api/reverse`
- Добавлено `if (path.equals("/reverse") || path.equals("/api/reverse"))`. Проблема 404 при автоопределении города устранена.

#### е) Маскирование ключей в GET и сохранение буллетов в POST
- Реализован метод `maskKey()`: оставляет первые и последние 4 символа для длинных ключей.
- В POST `/api/settings` при обнаружении `•` или `*` восстанавливается существующий ключ из `existing.optString(kf, "")`. При передаче пустой строки `""` ключ корректно очищается.

---

### 3.4. `android/app/src/main/java/ru/weather/informer/InformerServer.java`

- Список источников `primary_source` расширен:
  `server` (Локальный сервер), `yandex`, `gismeteo`, `foreca`, `om`, `owm`, `7timer`, `wttr`.
- Добавлены кнопки очистки `[✕]` для `gismeteo_api_key` и `api` (Яндекс).
- При клике на `[✕]` инпут очищается, и `currentCfg.api = ""` передается в POST.

---

### 3.5. `caching_server.py`

#### а) Дефект обновления токена Foreca (CRITICAL BUG-02)
В `caching_server.py`:
```python
FORECA.token = cfg.get("foreca_api_key", "")
```
Но в `foreca_provider.py` класс `ForecaProvider` инициализирует и использует только `self._token`:
```python
class ForecaProvider:
    def __init__(self, token, ...):
        self._token = (token or "").strip()
    ...
    def _http_get(self, url: str, timeout: float) -> bytes:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            ...
        })
```
В `ForecaProvider` **нет сеттера `token`**! Присваивание `FORECA.token = ...` просто создает динамическое поле `token` у экземпляра объекта, тогда как метод `_http_get()` читает `self._token`. В результате обновленный токен никогда не попадает в HTTP-запрос!
**Решение:** Добавить `@property` и `@token.setter` в `ForecaProvider` либо явно присваивать `FORECA._token = ...`.

#### б) Обработка источников `primary_source`
- Блок `if primary_source == ...` в `caching_server.py` теперь обрабатывает:
  - `"gismeteo"` -> `_try_gismeteo_v2()`
  - `"foreca"` -> `FORECA.get_weather(...)`
  - `"om"` -> `fetch_from_openmeteo(...)`
  - `"owm"` -> `fetch_from_openweathermap(...)`
  - `"7timer"` -> `fetch_from_7timer(...)`
  - `"wttr"` -> `fetch_from_wttr(...)`
  - `"yandex"` / default -> `_try_yandex()`, затем цепочка резервных источников.
- Кэширование каждого источника происходит с интервалом `ttl=interval`, прогноз фиксируется в логах точности (`record_forecast`).

#### в) Маскирование и безопасность `save_config`
- `mask_key` маскирует токены перед отправкой в GET `/api/settings`.
- В `save_config()` поля из списка `key_fields` с символами `•` или `*` игнорируются, сохраняя исходные секреты.
- В `do_POST` список `allowed_fields` дополнен полями `foreca_api_key`, `openweathermap_api_key`, `server_url`.

---

### 3.6. `android/build-apk.sh`

- Удалено копирование файла `$ROOT_DIR/config.json` в `$ASSETS_DIR/config.json`.
- В репозитории в `android/app/src/main/assets/config.json` сохранен чистый шаблон с пустыми ключами.
- Проверено содержимое собранного артефакта `apks/informer.apk`: ключи отсутствуют, приватные данные в APK не попадают. Сборка через `javac`, `d8`, `zipalign`, `apksigner` завершается без ошибок (exit code 0).

---

## 4. Результаты прогона тестового набора

Запуск `bash tests/run-all.sh`:
```text
=== node icon-test ===
ИТОГ: pass=39 fail=0
=== node converters-test ===
ИТОГ: pass=71 fail=0
=== node ui-test ===
ИТОГ: pass=72 fail=0
=== python gismeteo ===
OK
=== python gismeteo_v2 ===
OK
=== python foreca ===
OK
=== python synop ===
OK
=== python accuracy ===
OK
=== python history/7timer ===
OK
=== python fallback hierarchy ===
OK
=== ВСЁ ЗЕЛЁНОЕ ===
```
Регрессий в существующих модулях не обнаружено.

---

## 5. Рекомендованные патчи для устранения дефектов

### Патч 1: Поглощение `force=1` в `informer.html` и `assets/informer.html` (Fix BUG-01)
```javascript
// В начале informer.html (глобальные переменные):
var force_param_consumed = false;

// В функции run():
var isForce = false;
if (!force_param_consumed && window.location && window.location.search && window.location.search.indexOf("force=1") !== -1) {
    isForce = true;
    force_param_consumed = true;
    if (window.history && window.history.replaceState) {
        try {
            window.history.replaceState({}, document.title, window.location.pathname);
        } catch (e) {}
    }
}
```

### Патч 2: Сеттер токена в `foreca_provider.py` (Fix BUG-02)
```python
# В foreca_provider.py в класс ForecaProvider:
@property
def token(self) -> str:
    return self._token

@token.setter
def token(self, value: str) -> None:
    self._token = (value or "").strip()
```

### Патч 3: Безопасная обработка ошибок и запрет `config*.json` в `ClientHandler.java` (Fix SEC-01, SEC-02, SEC-03)
```java
// 1. В POST /api/settings:
} catch (Exception e) {
    Log.e(TAG, "Failed to parse/save settings: " + e.getMessage());
    sendResponse(out, 400, "Bad Request", "application/json; charset=utf-8",
            "{\"error\":\"Invalid settings payload\"}".getBytes(StandardCharsets.UTF_8));
    return;
}

// 2. В serveFile (защита .tmp и .json):
if (filename.startsWith("config.json")) {
    InetAddress addr = socket.getInetAddress();
    boolean isLocal = addr != null && (addr.isLoopbackAddress() || "127.0.0.1".equals(addr.getHostAddress()));
    if (!isLocal) {
        sendResponse(out, 403, "Forbidden", "text/plain",
                "Direct access to config.json is restricted. Use /api/settings".getBytes(StandardCharsets.UTF_8));
        return;
    }
}

// 3. В handleFileUpload (запрет перезаписи конфигурации):
if (filename.equalsIgnoreCase("config.json") || filename.toLowerCase(Locale.US).startsWith("config.")) {
    sendResponse(out, 403, "Forbidden", "application/json",
            "{\"error\":\"Direct upload of config files is forbidden. Use /api/settings\"}".getBytes(StandardCharsets.UTF_8));
    return;
}
```
