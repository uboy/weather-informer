# Архитектурное и Security/Regression Ревью (ARCHITECT_REVIEW_5)
**Проект:** Weather Informer (`/home/dmazur/services/weather-informer`)  
**Ветка:** `feature/tablet-webserver-gismeteo-deploy`  
**Дата ревью:** 1 октября 2026 г.  
**Роль:** Principal Software Architect & Embedded Systems / Security Reviewer  

---

## 1. Итоговый вердикт (Verdict)

### **APPROVED (Одобрено к развертыванию)**

Все дефекты и уязвимости, зафиксированные в `ARCHITECT_REVIEW_4` (BUG-01, BUG-02, SEC-01, SEC-02, SEC-03, UX-01), **полностью и корректно устранены**.
- Набор регрессионных и интеграционных тестов `tests/run-all.sh` завершился со 100% успехом (**pass=182, fail=0**, все Python-модули зеленые).
- Сборка Android APK (`android/build-apk.sh`) проходит без ошибок; получен подписанный чистый артефакт `apks/informer.apk` без утечки секретных API-токенов в assets.
- Обеспечена строгая обратная совместимость с WebView старых версий Android (5.0–7.0 / KitKat–Nougat, ES5-only).
- Поверхность атаки локального веб-сервера планшета закрыта от перезаписи конфигурации и несанкционированного скачивания файлов настроек через LAN.

---

## 2. Матрица верификации исправлений

| ID | Уровень | Компонент | Требование / Проблема | Статус проверки | Результат |
|---|---|---|---|---|---|
| **BUG-01** | **CRITICAL** | `informer.html`, `assets/informer.html` | Утечка `?force=1`: ежеминутный сброс кэша и флуд API внешних провайдеров. | **VERIFIED & FIXED** | Флаг `force_consumed` поглощается однократно; URL очищается через `history.replaceState`. Регулярный таймер раз в минуту не вызывает внеплановый опрос. |
| **BUG-02** | **CRITICAL** | `foreca_provider.py` | Отсутствие сеттера `token` в `ForecaProvider`; невозможность динамического обновления ключа. | **VERIFIED & FIXED** | Добавлены `@property def token(self)` и `@token.setter def token(self, value)`, обновляющие внутренний `self._token`. |
| **SEC-01** | **HIGH** | `ClientHandler.java` | Блок `catch` в `POST /api/settings` затирал `config.json` сырым `body`. | **VERIFIED & FIXED** | Ошибки парсинга/валидации возвращают HTTP 400 Bad Request без модификации существующего `config.json`. |
| **SEC-02** | **HIGH** | `ClientHandler.java` | Эндпоинт `/api/upload` позволял перезаписать `config.json` по LAN. | **VERIFIED & FIXED** | Добавлена проверка `lower.startsWith("config") \|\| lower.equals("config.json")` с возвратом HTTP 403 Forbidden. |
| **SEC-03** | **MEDIUM** | `ClientHandler.java` | Прямой доступ к `config.json.tmp` отдавал файл настроек без проверки LAN. | **VERIFIED & FIXED** | Проверка `filename.toLowerCase(Locale.US).startsWith("config")` блокирует как `config.json`, так и `.tmp`, `.bak` для не-loopback клиентов. |
| **UX-01** | **MEDIUM** | `InformerServer.java`, `caching_server.py` | Отсутствие полей `foreca_api_key` и `openweathermap_api_key` и кнопок сброса `[✕]` в веб-настройках. | **VERIFIED & FIXED** | Добавлены инпуты для Foreca и OWM, кнопки `[✕]`, очистка и передача в `doSave()` на планшете и сервере. |

---

## 3. Детальный разбор верификации по компонентам

### 3.1. Клиентский фронтенд: `informer.html` и `android/app/src/main/assets/informer.html`
1. **Идентичность файлов:**
   Команда `diff -u informer.html android/app/src/main/assets/informer.html` возвращает 0 (побайтовое совпадение).
2. **Ликвидация утечки `?force=1` (BUG-01):**
   - В глобальной области объявлен флаг `var force_consumed = false;`.
   - В теле `run()` проверка форсирования защищена условием:
     ```javascript
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
     if ((isForce || lastcall < now - _effTimeout) && (server_url.trim() !== '' || api != 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')) {
         get_data();
     }
     ```
   - При первом запуске `force_consumed` становится `true`, а URL очищается от query-параметров через `history.replaceState`. При последующих вызовах `run()` раз в минуту `isForce` гарантированно `false`. Опрос внешних API строго подчиняется интервалу кэширования (`timeout` / `direct_timeout`). Квоты API защищены от исчерпания.
3. **Строгая ES5-совместимость:**
   - Проведен статический анализ содержимого всех 6 тегов `<script>` в `informer.html` (AST/синтаксический разбор через Node.js).
   - Подтверждено отсутствие синтаксических конструкций ES6+:
     - `const` — 0 вхождений;
     - `let` — 0 вхождений;
     - стрелочные функции `=>` — 0 вхождений;
     - шаблонные строки (backticks) — 0 вхождений;
     - `async` / `await` — 0 вхождений;
     - `class` — 0 вхождений.
   - Скрипты полностью совместимы со стоковым Chromium WebView 44+ в Android 5.0–7.0.

---

### 3.2. Провайдер Foreca: `foreca_provider.py`
1. **Реализация геттера и сеттера (BUG-02):**
   - В класс `ForecaProvider` добавлены свойства:
     ```python
     @property
     def token(self):
         return self._token

     @token.setter
     def token(self, value):
         self._token = (value or "").strip()
     ```
   - Метод `_http_get()` использует заголовок `"Authorization": f"Bearer {self._token}"`.
   - При вызовах `FORECA.token = cfg.get("foreca_api_key", "")` в `caching_server.py` (как при первоначальной загрузке, так и при динамическом обновлении через веб-форму) приватное поле `self._token` корректно мутирует, гарантируя валидную авторизацию запросов к `weatherapi.foreca.net`.

---

### 3.3. HTTP-сервер планшета: `ClientHandler.java`
1. **Безопасная обработка сбоев при сохранении настроек (SEC-01):**
   - Устранен опасный блок перезаписи файла `body` в секции `catch`.
   - При поступлении поврежденного JSON или возникновении исключения:
     ```java
     } catch (Exception e) {
         Log.e(TAG, "Failed to parse/save settings: " + e.getMessage());
         sendResponse(out, 400, "Bad Request", "application/json; charset=utf-8",
                 "{\"error\":\"Invalid settings payload or JSON error\"}".getBytes(StandardCharsets.UTF_8));
         return;
     }
     ```
   - Рабочий файл `config.json` не модифицируется, клиенту отдается информативный HTTP 400.
2. **Защита от загрузки файлов конфигурации через `/api/upload` (SEC-02):**
   - В методе `handleFileUpload`:
     ```java
     filename = new File(filename).getName();
     String lower = filename.toLowerCase(Locale.US);
     if (lower.startsWith("config") || lower.equals("config.json")) {
         sendResponse(out, 403, "Forbidden", "application/json",
                 "{\"error\":\"Uploading configuration files via /api/upload is prohibited. Use /api/settings\"}".getBytes(StandardCharsets.UTF_8));
         return;
     }
     ```
   - Заблокирована прямая загрузка любых файлов с префиксом `config*`.
   - Сохранена проверка на допустимые расширения (`.html`, `.js`, `.css`, `.json`, `.png`, `.svg`) и защита от Path Traversal (`dest.getCanonicalPath().startsWith(...)`).
3. **Блокировка доступа по LAN к файлам настроек (SEC-03):**
   - В методе `serveFile`:
     ```java
     if (filename.toLowerCase(Locale.US).startsWith("config")) {
         InetAddress addr = socket.getInetAddress();
         boolean isLocal = addr != null && (addr.isLoopbackAddress() || "127.0.0.1".equals(addr.getHostAddress()));
         if (!isLocal) {
             sendResponse(out, 403, "Forbidden", "text/plain",
                     "Direct access to config files is restricted. Use /api/settings".getBytes(StandardCharsets.UTF_8));
             return;
         }
     }
     ```
   - Прямой запрос к `/config.json`, `/config.json.tmp`, `/config.bak` из внешней локальной сети пресекается с кодом HTTP 403 Forbidden. Доступ возможен только локально с устройства (127.0.0.1), либо через маскированный эндпоинт `/api/settings`.

---

### 3.4. Веб-интерфейс настроек: `InformerServer.java` и `caching_server.py`
1. **Синхронизация полей API-ключей (UX-01):**
   - В HTML-форму настроек обоих серверов (внутреннего на Android и внешнего на Python) добавлены поля:
     - `foreca_api_key` (Foreca API Token / Bearer);
     - `openweathermap_api_key` (OpenWeatherMap API Key).
   - Для каждого поля добавлена кнопка быстрой очистки `[✕]` (`btn_clear_foreca`, `btn_clear_owm`).
   - Обработчики кнопок очистки сбрасывают значение визуального инпута и очищают соответствующее поле в объекте `currentCfg`.
   - В функции `doSave()` оба значения считываются (`document.getElementById(...).value.trim()`) и передаются в POST `/api/settings`.
   - В обработчиках сервера (`save_config` в Python и `POST /api/settings` в Java) маскированные буллеты (`•` и `*`) корректно сохраняют существующие секреты, а пустая строка стирает ключ.

---

## 4. Результаты тестового запуска (Regression Verification)

### 4.1. Автоматизированные тесты `tests/run-all.sh`
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
**Итог:** 182 UI/converter/icon теста успешно пройдены, 7 интеграционных тестов провайдеров завершились со статусом OK. Регрессий не обнаружено.

### 4.2. Сборка мобильного приложения `android/build-apk.sh`
- Компиляция ресурсов `aapt2`: OK
- Линковка и генерация `R.java`: OK
- Компиляция Java 8 через `javac`: OK
- Трансляция в Dalvik DEX через `d8`: OK
- Выравнивание `zipalign`: OK
- Подпись `apksigner`: OK
- Артефакт: `/home/dmazur/services/weather-informer/apks/informer.apk` (97 KB).
- Проверка содержимого APK: в `assets/config.json` все секретные токены (`api`, `gismeteo_api_key`, `foreca_api_key`, `openweathermap_api_key`) пусты. Утечка производственных ключей в бинарный дистрибутив отсутствует.

---

## 5. Заключение

Архитектура системы, сетевое взаимодействие и безопасность локального сервера соответствуют стандартам надежности встраиваемых Linux/Android киосков. Кодовая база готова к развертыванию на целевых устройствах.
