#!/usr/bin/env node
/**
 * ui-test.js — структурные инварианты HTML/CSS/рендера.
 * Без браузера: парсим DOM-структуру, CSS-правила и JS-логику.
 * Каждый прошлый визуальный баг здесь как регрессионный вектор.
 */

const fs = require('fs');
const html = fs.readFileSync(__dirname + '/../informer.html', 'utf8');
let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log('  [OK] ' + name); }
    else { fail++; console.log('  [FAIL] ' + name + (extra ? ' — ' + extra : '')); }
}

// ===== 1. HTML-структура =====
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);
const dupIds = ids.filter((v, i) => ids.indexOf(v) !== i);
check('все id уникальны', dupIds.length === 0, 'дубликаты: ' + dupIds.join(','));

for (const el of ['temperature', 'feels', 'feels_label', 'min', 'max', 'min_date', 'max_date',
    'wind', 'humidity', 'pressure', 'rise', 'set', 'duration_hour', 'duration_minute',
    'duration_left', 'status_bar', 'weather_menu', 'key_dialog', 'city_search_input',
    'forecast_icon_0', 'forecast_icon_1', 'moon']) {
    check('элемент #' + el + ' существует', ids.includes(el));
}

// ===== 2. CSS-инварианты =====
// 2.1 temperature_sub — flex (регрессия: пропал display:flex при патче feels-like)
const subCss = html.match(/#temperature_sub\s*\{[^}]*\}/g) || [];
const baseSub = subCss.find(c => !c.includes('!important'));
check('#temperature_sub имеет display:flex в базовом CSS',
    baseSub && /display:\s*flex/.test(baseSub),
    'найдено правил: ' + subCss.length);

// 2.2 z-index: key_dialog > menu > menu_btn > status_bar (регрессия z-index:300)
const zIdx = (sel) => {
    const m = html.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{[^}]*z-index:\\s*(\\d+)', 'm'));
    return m ? parseInt(m[1]) : null;
};
const zDialog = zIdx('#key_dialog');
const zMenu = zIdx('#weather_menu');
const zStatus = zIdx('#status_bar');
check('z-index: key_dialog (' + zDialog + ') > weather_menu (' + zMenu + ')',
    zDialog !== null && zMenu !== null && zDialog > zMenu);
check('z-index: weather_menu (' + zMenu + ') > status_bar (' + zStatus + ')',
    zStatus !== null && zMenu !== null && zMenu > zStatus);

// 2.3 один градус (::after) на feels, temperature, min, max — не в JS
const feelsLines = html.split('\n').filter(l => l.includes('result.feels'));
check('feels в JS С ° (градус в значении, не CSS)',
    feelsLines.length > 0 && feelsLines.every(l => l.includes("'°'")),
    JSON.stringify(feelsLines.map(l => l.trim().slice(0, 60))));
check('#feels НЕ в CSS ::after (градус из JS)', !html.match(/#feels::after/));

// 2.4 nowrap на sub-row (регрессия «порвана строка»)
const nowrap = html.match(/#temperature_sub div,\s*#temperature_sub span\s*\{[^}]*white-space:\s*nowrap/s);
check('#temperature_sub nowrap (анти-перенос)', !!nowrap);

// 2.5 feels_label без собственного font-size (наследует от div — регрессия 2vw vs 1.6vw)
const labelCss = html.match(/\.sector_feels\s*\{[^}]*\}/);
check('.sector_feels без font-size (наследование)', labelCss && !/font-size/.test(labelCss[0]),
    labelCss ? labelCss[0] : 'не найден');

// ===== 3. JS-инварианты =====
// 3.1 calc_feels_like: векторы
const flMatch = html.match(/^function calc_feels_like[\s\S]*?^\}/m);
check('calc_feels_like извлечена', !!flMatch);
if (flMatch) {
    const fn = eval('(function(){' + flMatch[0] + '; return calc_feels_like;})()');
    check('feels_like: null → null', fn(null, 50, 3) === null);
    check('feels_like: undefined → null', fn(undefined, 50, 3) === null);
    check('feels_like: NaN → null', fn('abc', 50, 3) === null);
    check('feels_like: 0°/3м/с(10.8км/ч) → −4 (км/ч-формула)', fn(0, 50, 3) <= -3 && fn(0, 50, 3) >= -5, String(fn(0, 50, 3)));
    check('feels_like: −10°/5м/с(18км/ч) → −17±2', fn(-10, 50, 5) <= -15 && fn(-10, 50, 5) >= -19, String(fn(-10, 50, 5)));
    check('feels_like: −5°/8м/с(29км/ч) → −14±2', fn(-5, 80, 8) <= -12 && fn(-5, 80, 8) >= -16, String(fn(-5, 80, 8)));
    check('feels_like: Foreca hum=null → чилл работает', fn(5, null, 2) != null, String(fn(5, null, 2)));
    check('feels_like: граница t=10 слабый ветер ~факт', Math.abs(fn(10, 50, 1) - 10) <= 1, String(fn(10, 50, 1)));
    check('feels_like: граница t=27/hum=39 → факт', fn(27, 39, 2) === 27, String(fn(27, 39, 2)));
    check('feels_like: жара 30°/70% → ≥ +32', fn(30, 70, 2) >= 32, String(fn(30, 70, 2)));
    check('feels_like: жара 30°/30% → факт', fn(30, 30, 2) === 30, String(fn(30, 30, 2)));
}

// 3.2 astro_sun: polar guard
const astroMatch = html.match(/^function astro_sun[\s\S]*?^\}/m);
check('astro_sun извлечена', !!astroMatch);
if (astroMatch) {
    const fn = eval('(function(){' + astroMatch[0] + '; return astro_sun;})()');
    const nn = fn(56.3177, 43.9993, new Date());
    check('astro_sun: НН — числа', nn.sunrise && nn.sunset && nn.sunrise.includes(':') && nn.sunset.includes(':'));
    const polar = fn(78.2, 15.6, new Date(2026, 5, 21)); // Мурманск, лето — полярный день
    check('astro_sun: полярный день → null', polar.sunrise === null || polar.sunset === null,
        JSON.stringify(polar));
}

// 3.3 render_weather: duration не нулевой при валидном солнце (регрессия 00ч00м)
// Извлекаем и выполняем render_weather с мок-данными
const renderMatch = html.match(/^function render_weather[\s\S]*?^\}/m);
check('render_weather извлечена', !!renderMatch);
if (renderMatch) {
    // Проверяем что сброс duration_* только в полярном else (после астрономии)
    const renderSrc = renderMatch[0];
    const astroIdx = renderSrc.indexOf('astro_sun');
    const resetIdx = renderSrc.indexOf("result.duration_hour = '00'");
    check('duration reset только после astronomy (не перед)', 
        astroIdx > 0 && resetIdx > 0 && resetIdx > astroIdx,
        'astro=' + astroIdx + ' reset=' + resetIdx);
    // Нет преждевременного сброса (до секции астрономии)
    const beforeAstro = renderSrc.substring(0, astroIdx);
    check('нет duration_hour=00 до astronomy', !beforeAstro.includes("duration_hour = '00'"));
}

// 3.3b riseset: длительность и обратный отсчёт дня/ночи (регрессия: ночь 29:50 после полуночи)
const risesetMatch = html.match(/^function riseset[\s\S]*?^\}/m);
check('riseset извлечена', !!risesetMatch);
if (risesetMatch) {
    const fn = eval('(function(){' +
        'var value = {}, period = {day_day: "до заката", day_night: "до рассвета"}, date, pad = function(n){return n<10?"0"+n:n;};' +
        'var lastResult = null, update = function(r){ lastResult = r; };' +
        risesetMatch[0] + ';' +
        'return function(riseStr, setStr, h, m) {' +
            'date = { getHours: function() { return h; }, getMinutes: function() { return m; } };' +
            'riseset(riseStr, setStr);' +
            'return lastResult;' +
        '};' +
    '})()');

    // День: 12:00, восход 06:00, закат 18:00 -> осталось дня 6ч, длина дня 12:00
    const dayRes = fn('06:00', '18:00', 12, 0);
    check('riseset: день 12:00 -> до заката 06:00', dayRes && dayRes.duration_left === 'до заката 06:00', JSON.stringify(dayRes));
    check('riseset: день 12:00 -> длительность дня 12:00', dayRes && dayRes.duration_hour === '12' && dayRes.duration_minute === '00', JSON.stringify(dayRes));

    // День асимметричный: 10:00, восход 05:56, закат 17:54 -> длительность дня 11ч 58мин
    const dayAsym = fn('05:56', '17:54', 10, 0);
    check('riseset: день 10:00 -> длительность дня 11ч 58мин', dayAsym && dayAsym.duration_hour === '11' && dayAsym.duration_minute === '58', JSON.stringify(dayAsym));

    // Ночь асимметричная до полуночи: 22:00, восход 05:56, закат 17:54 -> длительность ночи 12ч 02мин (24:00 - 11:58)
    const nightEveAsym = fn('05:56', '17:54', 22, 0);
    check('riseset: ночь 22:00 -> длительность ночи 12ч 02мин', nightEveAsym && nightEveAsym.duration_hour === '12' && nightEveAsym.duration_minute === '02', JSON.stringify(nightEveAsym));
    check('riseset: ночь 22:00 -> duration_time сохраняет дневную ширину (50%)', nightEveAsym && nightEveAsym.duration_time.includes('width: 50%'), JSON.stringify(nightEveAsym));

    // Ночь после полуночи: 00:25, восход 06:15, закат 18:20 -> осталось ночи 5ч 50м, длина ночи 11ч 55м (день 12:05)
    const nightMornRes = fn('06:15', '18:20', 0, 25);
    check('riseset: ночь 00:25 -> до рассвета 05:50 (не 29:50)', nightMornRes && nightMornRes.duration_left === 'до рассвета 05:50', JSON.stringify(nightMornRes));
    check('riseset: ночь 00:25 -> длительность ночи 11ч 55мин', nightMornRes && nightMornRes.duration_hour === '11' && nightMornRes.duration_minute === '55', JSON.stringify(nightMornRes));

    // Полночь ровно: 00:00, восход 06:00, закат 18:00 -> до рассвета 06:00
    const midnightRes = fn('06:00', '18:00', 0, 0);
    check('riseset: полночь 00:00 -> до рассвета 06:00', midnightRes && midnightRes.duration_left === 'до рассвета 06:00', JSON.stringify(midnightRes));
    check('riseset: полночь 00:00 -> длительность ночи 12:00', midnightRes && midnightRes.duration_hour === '12' && midnightRes.duration_minute === '00', JSON.stringify(midnightRes));
}

// 3.4 ES5-дисциплина (регрессия при добавлении фич)
const es6 = [...html.matchAll(/\b(?:=>|`|\blet\s|\bconst\s|class\s+\w)/g)];
check('ES5: нет стрелок/let/const/template', es6.length === 0,
    es6.length + ' вхождений: ' + es6.slice(0, 3).map(m => m[0]).join(','));

// 3.5 tomorrow-parts: у каждого конвертера есть parts2/tmr7/tmrwOm/bandsT
for (const [name, marker] of [['OWM', 'byTmrw'], ['OM', 'tmrwOm'], ['7timer', 'tmr7'], ['Foreca', 'parts2']]) {
    check(name + ': tomorrow-parts маркер ' + marker, html.includes(marker));
}

// ===== 4. CSS-селекторы: все target-элементы ::after существуют =====
const afterTargets = [...html.matchAll(/#([a-z_]+)::after/g)].map(m => m[1]);
const deadAfter = ['temperature_alt']; // мёртвый CSS, известный
for (const t of [...new Set(afterTargets)]) {
    if (deadAfter.includes(t)) continue;
    check('::after #' + t + ' — элемент существует', ids.includes(t));
}

// ===== 5. Ключевые настройки по умолчанию =====
check('update_interval default 300 (5 мин)', /var timeout = 5 \* 60/.test(html));
check('direct_update_interval default 1800', /var direct_timeout = 30 \* 60/.test(html));

// ===== 6. Адаптивная вёрстка и высота видимой области =====
// 6.1 Динамическая подгонка высоты #wrapper под winH через setProperty с important
check('update_viewport_metrics: setProperty height winH important',
    /wrapper\.style\.setProperty\(\s*["']height["']\s*,\s*winH\s*\+\s*["']px["']\s*,\s*["']important["']\s*\)/.test(html));

// 6.2 Определение двойных системных панелей (top + bottom bar)
check('update_viewport_metrics: dual bar diffH >= 54',
    /diffH\s*>=\s*54/.test(html));

// 6.3 Инвариант бюджета высоты: datetime + weather_body <= 96vh (запас >= 4vh под status_bar)
const tallDtMatch = html.match(/html\.screen_tall\s+#datetime\s*\{[^}]*height:\s*([\d.]+)vh/);
const tallWbMatch = html.match(/html\.screen_tall\s+#weather_body\s*\{[^}]*height:\s*([\d.]+)vh/);
const tallDtH = tallDtMatch ? parseFloat(tallDtMatch[1]) : 0;
const tallWbH = tallWbMatch ? parseFloat(tallWbMatch[1]) : 0;
check('screen_tall: бюджет высоты (datetime ' + tallDtH + 'vh + weather_body ' + tallWbH + 'vh = ' + (tallDtH + tallWbH) + 'vh <= 96vh)',
    tallDtH > 0 && tallWbH > 0 && (tallDtH + tallWbH) <= 96);

const wideDtMatch = html.match(/#datetime\s*\{[^}]*height:\s*([\d.]+)vh/);
const wideWbMatch = html.match(/#weather_body\s*\{[^}]*height:\s*([\d.]+)vh/);
const wideDtH = wideDtMatch ? parseFloat(wideDtMatch[1]) : 0;
const wideWbH = wideWbMatch ? parseFloat(wideWbMatch[1]) : 0;
check('landscape widescreen: бюджет высоты (datetime ' + wideDtH + 'vh + weather_body ' + wideWbH + 'vh = ' + (wideDtH + wideWbH) + 'vh <= 96vh)',
    wideDtH > 0 && wideWbH > 0 && (wideDtH + wideWbH) <= 96);



// ===== 7. Интервалы синхронизации (карточка task-weather-informer-sync-intervals) =====
// 7.1 Дефолты: сервер 5 мин, внешние 30 мин — два разных интервала
check('дефолт опроса сервера = 5*60 сек (update_interval_sec)',
    /var timeout = 5 \* 60;/.test(html));
check('дефолт внешних запросов = 30*60 сек (direct_update_interval_sec)',
    /var direct_timeout = 30 \* 60;/.test(html));

// 7.2 Отдельный счётчик внешних попыток: падение сервера не должно сжигать
// квоту Яндекса с частотой серверного опроса
check('гейт внешних запросов: счётчик weather_last_direct_call',
    html.includes('weather_last_direct_call'));
check('on_failure (дефолтная цепочка) гейтится direct_gate_ok()',
    /function on_failure[\s\S]{0,900}?direct_gate_ok\(\)/.test(html));
check('try_direct_gismeteo гейтится direct_gate_ok()',
    /function try_direct_gismeteo[\s\S]{0,700}?direct_gate_ok\(\)/.test(html));
check('гейт помечает попытку: direct_gate_mark() определён',
    /function direct_gate_mark\(\)/.test(html));

// 7.3 «Обновить сейчас» (force=1) обходит гейт
check('force обходит гейт внешних запросов',
    /force_bypass_direct_gate = true/.test(html) &&
    /!force_bypass_direct_gate && !direct_gate_ok\(\)/.test(html));

// 7.4 Конфиг-ключи читаются в обоих путях загрузки (applyConfig/apply_config)
const cfgKeyCount = (html.match(/cfg\.update_interval_sec/g) || []).length;
check('конфиг: update_interval_sec читается (путей: ' + cfgKeyCount + ')', cfgKeyCount >= 2);
const dCfgKeyCount = (html.match(/cfg\.direct_update_interval_sec/g) || []).length;
check('конфиг: direct_update_interval_sec читается (путей: ' + dCfgKeyCount + ')', dCfgKeyCount >= 2);

// 7.5 Без сервера и с allow_direct_yandex основной источник — внешний: интервал внешних
check('нет сервера -> прямой источник использует direct_timeout',
    /server_url\.trim\(\) === "" && allow_direct_yandex/.test(html));

// 7.6 ES5-канарейка: ES6-конструкции в разметке/скрипте запрещены (Android 6.0 WebView)
check('ES5: нет стрелочных функций', !/=>/.test(html));
check('ES5: нет const/let объявлений', !/\b(const|let)\s+[A-Za-z_$]/.test(html));
check('ES5: нет шаблонных строк', !html.includes('`'));

console.log('\nИТОГ: pass=' + pass + ' fail=' + fail);
process.exit(fail ? 1 : 0);
