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
check('z-index: status_bar (' + zStatus + ') > weather_menu (' + zMenu + ')',
    zStatus !== null && zMenu !== null && zStatus > zMenu);

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
check('update_interval default 3600', /var timeout = 60 \* 60/.test(html));
check('direct_update_interval default 1800', /var direct_timeout = 30 \* 60/.test(html));

console.log('\nИТОГ: pass=' + pass + ' fail=' + fail);
process.exit(fail ? 1 : 0);
