# Архитектурное и Security/Quality Ревью (ARCHITECT_REVIEW_6)
**Проект:** Weather Informer (`/home/dmazur/services/weather-informer`)  
**Ветка:** `feature/tablet-webserver-gismeteo-deploy`  
**Дата ревью:** 1 октября 2026 г.  
**Роль:** Principal Software Architect & Systems / Security Reviewer  

---

## 1. Итоговый вердикт (Executive Verdict)

### **CHANGES REQUESTED (Требуются точечные исправления перед релизом)**

В ходе углубленного ревью изменений выявлен **один критический архитектурно-функциональный дефект (CRIT-01)** и **одна уязвимость fail-open (SEC-FAIL-01)**, препятствующие автономной работе клиентского информера на планшете при использовании прямых API-ключей провайдеров погоды:

1. **[CRITICAL BUG / ARCH-01] Блокировка клиентских API-ключей локального WebView из-за тотального маскирования в `GET /api/settings`:**  
   Фронтенд `informer.html` теперь загружает настройки через эндпоинт `/api/settings` (с фолбэком на `config.json`). Однако метод `ClientHandler.sendMaskedSettings()` маскирует ключи (`••••`) абсолютно для всех входящих соединений без проверки источника запроса (`isLocal`). Локальный WebView планшета (`127.0.0.1:8080`) получает маскированные ключи, метод `applyConfig()` их справедливо игнорирует (`indexOf('•') !== -1`), а вызов `config.json` не срабатывает из-за успешного HTTP 200 от `/api/settings`. В результате в автономном режиме (без домашнего сервера кэширования) прямой опрос и фолбэк на Gismeteo, Foreca, Yandex и OpenWeatherMap полностью ломаются (ключи остаются пустыми).
2. **[HIGH SECURITY / SEC-FAIL-01] Fail-Open утечка ключей в `ClientHandler.sendMaskedSettings()`:**  
   В блоке `catch (Exception e)` при ошибке парсинга JSON клиенту по LAN отдаются сырые байты `cfgBytes` без маскирования, что в случае некорректного синтаксиса файла приводит к компрометации приватных токенов во внешнюю подсеть.

Остальные модули (ES5-совместимость, расчет ветра для завтрашних слотов в Open-Meteo, поглощение `force=1`, изоляция ключей в `build-apk.sh`, динамический сеттер Foreca) реализованы на высоком инженерном уровне и полностью соответствуют системным требованиям.

---

## 2. Сводная матрица проверок

| № | Компонент | Требование / Проверяемый аспект | Статус | Уровень критичности |
|---|---|---|---|---|
| **1.1** | `informer.html` | Загрузка `/api/settings` с фолбэком на `config.json` | ⚠️ **FAIL** | **CRITICAL** (ломает standalone-режим) |
| **1.2** | `informer.html` | Поглощение `force=1` и очистка URL через `history.replaceState` | ✅ **PASS** | LOW (защита от API-флуда) |
| **1.3** | `informer.html` | Расчет ветра (скорость/вектор) для завтрашних слотов Open-Meteo | ✅ **PASS** | MEDIUM |
| **1.4** | `informer.html` | Поддержка `primary_source` из конфига | ✅ **PASS** | MEDIUM |
| **1.5** | `informer.html` | Строгая ES5-совместимость (Android 5.0–7.0 WebView) | ✅ **PASS** | HIGH |
| **2.1** | `ClientHandler.java` | Безопасная отдача настроек: `sendMaskedSettings()` | ⚠️ **FAIL** | **HIGH** (fail-open при сбое JSON) |
| **2.2** | `ClientHandler.java` | Блокировка доступа к raw config файлам (`.tmp`, `.bak`) по LAN | ✅ **PASS** | HIGH |
| **2.3** | `ClientHandler.java` | Запрет загрузки `config*` через `/api/upload` | ✅ **PASS** | CRITICAL |
| **2.4** | `ClientHandler.java` | Атомарный `POST /api/settings` (HTTP 400 без затирания) | ✅ **PASS** | HIGH |
| **3.1** | `InformerServer` & `caching_server` | Поля `foreca`/`owm`, кнопки `[✕]`, сохранение в `doSave()` | ✅ **PASS** | MEDIUM |
| **3.2** | `foreca_provider.py` | Динамическое обновление token (property/setter) | 🟡 **PASS w/ REC** | LOW (пропуск в `fetch_observations`) |
| **4.1** | `build-apk.sh` | Изоляция боевых ключей от assets APK | ✅ **PASS** | CRITICAL |
| **5.1** | `tests/run-all.sh` | Прохождение тестов регрессии и конвертеров | ✅ **PASS** | HIGH (pass=182, fail=0) |

---

## 3. Детальный технический анализ изменений

### 3.1. Клиентский код: `informer.html` и `android/app/src/main/assets/informer.html`

1. **Идентичность файлов:**
   Файлы синхронизированы байт-в-байт (`diff informer.html android/app/src/main/assets/informer.html` возвращает код `0`).

2. **Анализ дефекта CRIT-01 (`/api/settings` vs `config.json`):**
   В коде `load_config()`:
   ```javascript
   var isHttp = (window.location && (window.location.protocol === "http:" || window.location.protocol === "https:"));
   var firstUrl = isHttp ? "/api/settings" : "config.json";
   $.getJSON(firstUrl).done(function(cfg) {
       applyConfig(cfg);
       if (callback) callback();
   }).fail(...)
   ```
   В `applyConfig(cfg)`:
   ```javascript
   if (cfg.api && cfg.api.indexOf('•') === -1 && ...) api = cfg.api;
   if (cfg.foreca_api_key !== undefined && cfg.foreca_api_key.indexOf('•') === -1) foreca_api_key_cfg = String(cfg.foreca_api_key);
   if (cfg.gismeteo_api_key !== undefined && cfg.gismeteo_api_key.indexOf('•') === -1) gismeteo_api_key_cfg = String(cfg.gismeteo_api_key);
   if (cfg.openweathermap_api_key !== undefined && cfg.openweathermap_api_key.indexOf('•') === -1) owm_key = cfg.openweathermap_api_key;
   ```
   **Проблема:**  
   Когда `MainActivity` запускает информер на планшете (`http://127.0.0.1:8080/?force=1`), `isHttp` истинно. Запрос уходит на `/api/settings`. Так как `ClientHandler` безусловно маскирует ключи для `/api/settings`, ответ содержит `"••••"`. Метод `applyConfig` отбрасывает эти значения. Поскольку запрос завершился со статусом 200, ветка `.fail()` (загрузка `config.json`) **никогда не выполняется**.  
   В итоге в рантайме планшета переменные `gismeteo_api_key_cfg`, `foreca_api_key_cfg`, `owm_key`, `api` остаются пустыми. При падении сервера кэширования либо при работе планшета без сервера:
   - Фолбэк на Gismeteo не добавляется в цепочку (`effGisKey` пуст);
   - Выбор Gismeteo как `primary_source` приводит к блокирующему всплывающему окну ввода ключа на экране киоска.

3. **Расчет ветра для завтрашних слотов (Open-Meteo):**
   - Реализована функция `partWind(h0, h1, targetDay)` с явным сопоставлением `targetDay || today`.
   - Векторное усреднение направления ветра выполнено математически строго через тригонометрические проекции:
     $$\text{meanRad} = \text{atan2}\left(\frac{\sum \sin \theta_i}{N}, \frac{\sum \cos \theta_i}{N}\right)$$
     с нормализацией в диапазон $[0, 360^\circ)$. Это исключает искажения при переходе через север ($359^\circ$ и $1^\circ$).
   - Вспомогательная функция `tmrwWind(h0, h1)` и вызовы `tmrwPart('night', 'bkn_n', 0, 6)` / `tmrwPart('morning', 'bkn_d', 6, 12)` корректно проставляют `wind_speed` и `wind_angle` в структуру `forecasts[1].parts`. Слоты «Ночью» и «Утром» для завтрашнего дня отображают корректную розу ветров.

4. **Параметр `force=1` и `history.replaceState`:**
   - Однократное потребление гарантировано флагом `force_consumed = true;`.
   - Вызов `window.history.replaceState({}, document.title, window.location.pathname)` безопасно обернут в `try/catch`. Повторные циклы `run()` (каждую секунду/минуту) не вызывают внеплановых сетевых запросов. Квоты внешних провайдеров защищены.

5. **Поддержка `primary_source`:**
   - Чтение свойства `cfg.primary_source` в `primary_source_cfg`.
   - Иерархия приоритетов: ручной выбор пользователя в меню (`localStorage.weather_source_pref`) имеет приоритет над конфигом, но если в `localStorage` значение пусто или `"default"`, активируется `primary_source_cfg`.

6. **ES5-совместимость:**
   - Проведен статический AST/Regex анализ тела скриптов в `informer.html`.
   - Конструкции ES6+ (`const`, `let`, `()=>{}`, шаблонные строки ` `` `, `async/await`, `class`) отсутствуют.
   - Скрипт полностью функционален в системном WebView Android 5.0 (API 21, Chromium 37/44) и 7.0 (Chromium 53).

---

### 3.2. HTTP-сервер планшета: `ClientHandler.java`

1. **Безопасная отдача настроек и разграничение доступа LAN vs Localhost:**
   В текущей реализации:
   ```java
   if (path.equals("/api/settings")) {
       if (method.equals("GET")) {
           sendMaskedSettings(out); // <- ОШИБКА: маскирует даже для 127.0.0.1
           return;
       }
   ...
   ```
   В то же время в `serveFile`:
   ```java
   if (filename.toLowerCase(Locale.US).startsWith("config")) {
       InetAddress addr = socket.getInetAddress();
       boolean isLocal = addr != null && (addr.isLoopbackAddress() || "127.0.0.1".equals(addr.getHostAddress()));
       if (!isLocal) {
           if (filename.equalsIgnoreCase("config.json")) {
               sendMaskedSettings(out);
               return;
           }
           sendResponse(out, 403, "Forbidden", ...);
           return;
       }
   }
   ```
   В `serveFile` логика разделения `isLocal` прописана корректно, а в маршруте `/api/settings` была упущена. Необходимо добавить проверку `isLocal` в обработчик `GET /api/settings`.

2. **Уязвимость в `sendMaskedSettings` (SEC-FAIL-01):**
   ```java
   private void sendMaskedSettings(OutputStream out) {
       byte[] cfgBytes = server.readConfigFile();
       try {
           JSONObject cfg = new JSONObject(new String(cfgBytes, StandardCharsets.UTF_8));
           ...
           sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfg.toString(2).getBytes(StandardCharsets.UTF_8));
       } catch (Exception e) {
           sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfgBytes); // <- УТЕЧКА!
       }
   }
   ```
   Если файл `config.json` окажется синтаксически поврежден или выбросит исключение при разборе, сервер вернет **сырой незамаскированный файл** клиенту по сети LAN. При сбое необходимо возвращать HTTP 500 с безопасным JSON-сообщением об ошибке.

3. **Защита от скачивания служебных файлов конфигурации:**
   - Любые файлы, начинающиеся с `config` (например, `config.json.tmp`, `config.bak`), при запросе из внешней сети блокируются с HTTP 403 Forbidden.
   - Имя файла предварительно очищается через `new File(filename).getName()`.

4. **Защита `/api/upload`:**
   - Запрещена загрузка любых файлов с префиксом `config` (`lower.startsWith("config")`).
   - Белый список расширений строго ограничен: `.html`, `.js`, `.css`, `.json`, `.png`, `.svg`.
   - Защита от Path Traversal реализована через каноническое сравнение путей: `dest.getCanonicalPath().startsWith(server.getWebDir().getCanonicalPath())`.
   - Лимит размера тела запроса (5 МБ) предотвращает DoS по памяти на планшетах с 1 ГБ RAM.

5. **Атомарность `POST /api/settings`:**
   - Сохранение выполняется через временный файл `config.json.tmp` с последующим `renameTo`.
   - Маскированные буллеты (`•` и `*`) не перезаписывают существующие боевые ключи.
   - При ошибке десериализации JSON возвращается HTTP 400 Bad Request, существующий `config.json` остается нетронутым.

---

### 3.3. Веб-интерфейс настроек и бэкенд: `InformerServer.java`, `caching_server.py`, `foreca_provider.py`

1. **Синхронизация полей ключей и кнопки сброса `[✕]`:**
   - На обоих серверах (Java и Python) добавлены поля `foreca_api_key` и `openweathermap_api_key`.
   - Кнопки `btn_clear_*` обнуляют значение инпута и соответствующее поле в JavaScript объекте `currentCfg`.
   - Сохранение пустого значения в `POST /api/settings` корректно очищает ключ в файле `config.json` (так как пустая строка не содержит `•` или `*`).

2. **Динамический сеттер токена Foreca:**
   - В `foreca_provider.py` добавлен property/setter для `token`:
     ```python
     @property
     def token(self):
         return self._token

     @token.setter
     def token(self, value):
         self._token = (value or "").strip()
     ```
   - В `caching_server.py` перед вызовами погоды Foreca выполняется `FORECA.token = cfg.get("foreca_api_key", "")`.
   - **Рекомендация:** В функции `fetch_observations()` (строка 453 `caching_server.py`) обращение к `FORECA.get_observations(...)` происходит без предварительного обновления `FORECA.token`. Рекомендуется добавить обновление токена также и в `fetch_observations()`, либо непосредственно в функцию `save_config()`.

---

### 3.4. Изоляция секретов в `android/build-apk.sh`

1. **Анализ сборочного конвейера:**
   - В `build-apk.sh` исключено копирование рабочего `$ROOT_DIR/config.json` в ассеты приложения.
   - В репозитории файл `android/app/src/main/assets/config.json` содержит только шаблонные параметры с пустыми строками для всех секретных ключей.
   - Проверено содержимое собранного артефакта `apks/informer.apk`:
     ```bash
     unzip -p apks/informer.apk assets/config.json
     ```
     Все ключи (`api`, `gismeteo_api_key`, `foreca_api_key`, `openweathermap_api_key`) гарантированно пусты `""`. Утечка приватных ключей разработчика в бинарный APK исключена.

---

### 3.5. Результаты тестирования (`tests/run-all.sh`)

Скрипт сквозного тестирования завершился со 100% успехом:
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

---

## 4. Конкретные рекомендации и необходимые правки (Action Items)

### 4.1. Обязательное исправление CRIT-01 и SEC-FAIL-01 в `ClientHandler.java`

В файле `android/app/src/main/java/ru/weather/informer/ClientHandler.java`:

```java
<<<<
        // 3. API настроек /api/settings
        if (path.equals("/api/settings")) {
            if (method.equals("GET")) {
                sendMaskedSettings(out);
                return;
            }
====
        // 3. API настроек /api/settings
        if (path.equals("/api/settings")) {
            if (method.equals("GET")) {
                InetAddress addr = socket.getInetAddress();
                boolean isLocal = addr != null && (addr.isLoopbackAddress() || "127.0.0.1".equals(addr.getHostAddress()));
                if (isLocal) {
                    byte[] cfgBytes = server.readConfigFile();
                    sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfgBytes);
                } else {
                    sendMaskedSettings(out);
                }
                return;
            }
>>>>
```

И в методе `sendMaskedSettings`:

```java
<<<<
        } catch (Exception e) {
            sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfgBytes);
        }
====
        } catch (Exception e) {
            Log.e(TAG, "Failed to mask settings: " + e.getMessage());
            sendResponse(out, 500, "Server Error", "application/json; charset=utf-8",
                    "{\"error\":\"Failed to process configuration\"}".getBytes(StandardCharsets.UTF_8));
        }
>>>>
```

### 4.2. Рекомендация по обновлению токена в `caching_server.py`

В функции `save_config` файла `caching_server.py`:
```python
    if "foreca_api_key" in current:
        FORECA.token = current.get("foreca_api_key", "")
```
Это гарантирует актуальность токена для всех фоновых воркеров (включая `fetch_observations`) сразу в момент сохранения формы без ожидания первого запроса погоды.

### 4.3. Рекомендация по расширению автотестов в `converters-test.js`

Добавить проверку скорости и направления ветра для завтрашних слотов Open-Meteo:
```javascript
check('OM: завтра-ночь wind_speed не null', r.result.forecasts[1].parts.night.wind_speed != null);
check('OM: завтра-ночь wind_angle в пределах 0-360', r.result.forecasts[1].parts.night.wind_angle >= 0 && r.result.forecasts[1].parts.night.wind_angle <= 360);
```

---

## 5. Заключение

Архитектура системы стала существенно чище и безопаснее по сравнению с предыдущими итерациями: боевые токены изолированы от дистрибутива APK, таймер `force=1` стабилизирован, а расчет векторов ветра для Open-Meteo завтрашнего дня математически выверен.

После внесения указанного двухстрочного исправления в `ClientHandler.java` (проверка `isLocal` в `GET /api/settings` и ликвидация fail-open утечки в `sendMaskedSettings`), код полностью готов к утверждению (**APPROVED**) и деплою на парк планшетов.
