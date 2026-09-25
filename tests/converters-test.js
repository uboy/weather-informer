#!/usr/bin/env node
/**
 * converters-test.js — энд-ту-енд прогон КЛИЕНТСКИХ конвертеров informer.html
 * на фикстурах API-ответов (форма как у живых источников).
 *
 * Причина появления: все регрессии 7 раундов ревью жили в конвертерах
 * (ReferenceError hIdx, OOB wttr [9]/[18], tzshift no-op, Foreca parts2 TDZ,
 * дневная иконка завтрашней ночи) — syntax-check их не ловил.
 * Правило: новый источник = фиксстура + прогон здесь; правка конвертера = прогон здесь.
 */

const fs = require('fs');
const html = fs.readFileSync(__dirname + '/../informer.html', 'utf8');
let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log('  [OK] ' + name); }
    else { fail++; console.log('  [FAIL] ' + name + (extra ? ' — ' + extra : '')); }
}

// --- извлечение топ-левел функций (flush-left "function NAME(..." до "^}") ---
function extractFn(name) {
    const re = new RegExp('^(?:var )?' + name.replace(/[$]/g, '[$]') + ' = function|function ' + name.replace(/[$]/g, '[$]') + '\\(', 'm');
    return html.match(re) ? name : null;
}
function srcOf(name) {
    // балансный парсер: от сигнатуры до парной закрывающей скобки (учёт строк и комментариев)
    const re = new RegExp('^[ \\t]?(?:var ' + name + ' = function|function ' + name + ')\\(', 'm');
    const m = html.match(re);
    if (!m) return null;
    const start = m.index;
    let i = html.indexOf('{', start);
    if (i < 0) return null;
    let depth = 0, q = null, prev = '', j = i;
    for (; j < html.length; j++) {
        const c = html[j];
        if (q) {
            if (c === '\\') { j++; continue; }
            if (c === q) q = null;
            continue;
        }
        if (prev === '/' && c === '/') { while (j < html.length && html[j] !== '\n') j++; prev = ''; continue; }
        if (prev === '/' && c === '*') { j = html.indexOf('*/', j) + 1; prev = ''; continue; }
        if (c === '\'' || c === '"' || c === '`') { q = c; prev = ''; continue; }
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) break; }
        prev = c;
    }
    return html.slice(start, j + 1);
}
const fnNames = ['safe_get_item', 'safe_set_item', 'moonPhaseCode', 'normalize_icon',
    'fetch_from_owm', 'fetch_from_om_client', 'fetch_from_7timer_client',
    'fetch_from_wttr', 'fetch_from_foreca_client', 'convert_foreca',
    'points_push', 'points_filter', 'points_each', 'points_each_day', 'foreca_icon'];
let code = '';
const aliasSrc = html.match(/^var ICON_ALIAS = \{[\s\S]*?^\};/m);
if (aliasSrc) code += aliasSrc[0] + '\n';
for (const n of fnNames) {
    const src = srcOf(n);
    check('извлечена функция ' + n, !!src);
    if (src) code += src + '\n';
}

// --- CSS-вокабуляр значков из <style> ---
const cssClasses = new Set();
for (const m of html.matchAll(/\.([a-z_0-9+-]+)\s*\{/g)) cssClasses.add(m[1]);
check('CSS-классы значков извлечены', cssClasses.size > 20, 'найдено ' + cssClasses.size);

// --- песочница ---
function pad2(n) { return (n < 10 ? '0' : '') + n; }
function dstr(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
function tstr(d) { return dstr(d) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':00'; }

const now = new Date();
const storage = {};
let fixtureFor = {}; // url-pattern -> payload or function(url)

const sandboxRequire = `
    var lat = 56.32, lon = 44.0, owm_key = 'TESTKEY', server_url = '', city_name_cfg = 'Nizhny Novgorod', foreca_api_key_cfg = 'TESTFORECA';
    var localStorage = {
        getItem: function(k) { return (k in storage) ? storage[k] : null; },
        setItem: function(k, v) { storage[k] = String(v); },
        removeItem: function(k) { delete storage[k]; }
    };
    var console = { log: function(){}, error: function(){} };
    var $ = {
        ajax: function(cfg) {
            function payload() {
                var p = null;
                for (var fi = 0; fi < fixtureFor.length; fi++) {
                    if (cfg.url.indexOf(fixtureFor[fi].match) >= 0) { p = fixtureFor[fi].payload; break; }
                }
                return p;
            }
            if (cfg.success) {
                try { cfg.success(payload()); } catch (e) { lastError = e; }
                if (cfg.error) { /* фиксстуры успеха: error не вызываем */ }
                return {};
            }
            return {
                done: function(f) {
                    try { f(payload()); } catch (e) { lastError = e; }
                    return this;
                },
                fail: function(f) { failCalled = true; return this; }
            };
        }
    };
    var lastError = null, failCalled = false;
`;

function runConvert(fnName, fixtures, timeoutMs) {
    const src = `
        (function() {
            ${sandboxRequire}
            ${code}
            return function(payloads) {
                fixtureFor = payloads;
                lastError = null; failCalled = false;
                var result = null, error = null;
                var done = false;
                try {
                    ${fnName}(function(d) { result = d; done = true; }, function(x, s, e) { error = (s || '') + ':' + (e || ''); done = true; });
                } catch (e) { error = 'THROW:' + e.message; done = true; }
                return { result: result, error: error, threw: lastError, failCalled: failCalled };
            };
        })()
    `;
    const factory = new Function('storage', 'fixtureFor', 'return (' + src.trim() + ')')();
    return factory(fixtures);
}

function iconsOk(parts) {
    if (!parts) return false;
    for (const k of ['night', 'morning', 'day', 'evening']) {
        const ic = parts[k] && parts[k].icon;
        if (ic == null) continue;
        if (!cssClasses.has(ic)) return false;
    }
    return true;
}
function hoursSane(hours) {
    return Array.isArray(hours) && hours.length > 0 && hours.every(h => h.temp === null || typeof h.temp === 'number');
}

// ============================= OWM =============================
(function () {
    const list = [];
    const city = { timezone: 3 * 3600, sunrise: Math.floor(now.getTime() / 1000) - 5 * 3600, sunset: Math.floor(now.getTime() / 1000) + 2 * 3600 };
    const tomorrow = new Date(now.getTime() + 24 * 3600000);
    for (let i = 0; i < 40; i++) {
        const d = new Date(now.getTime() + (i * 3 - 3) * 3600000); // блоки 3ч вокруг сейчас (локаль=город)
        // dt_txt: UTC-компоненты = local_d − city.tz (по компонентам, БЕЗ двойного сдвига toISOString)
        const utc = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), 0) - city.timezone * 1000);
        let temp = 100 + i; // сегодня: уникальные (факт = blocks[0] = 100)
        if (d.getDate() === tomorrow.getDate() && d.getMonth() === tomorrow.getMonth()) {
            if (d.getHours() < 6) temp = 7;       // завтра-ночь: константа
            else if (d.getHours() < 12) temp = 9; // завтра-утро: константа
        }
        list.push({
            dt_txt: utc.toISOString().slice(0, 19).replace('T', ' '),
            main: { temp: temp, humidity: 50, pressure: 1010 },
            wind: { speed: 2, deg: 120 },
            weather: [{ id: 800 }]
        });
    }
    // dt_txt формат YYYY-MM-DD HH:MM:SS
    for (const it of list) it.dt_txt = it.dt_txt.replace('T', ' ');
    const r = runConvert('fetch_from_owm', [
        { match: 'api.openweathermap.org/data/2.5/forecast', payload: { list: list, city: city } }
    ]);
    check('OWM: конвертация без ошибок', !r.error && r.result, r.error || (r.threw ? 'THROW:' + r.threw.message : ''));
    if (r.result) {
        check('OWM: fact = первый блок (100)', r.result.fact && r.result.fact.temp === 100, JSON.stringify(r.result.fact && r.result.fact.temp));
        check('OWM: завтра-ночь temp = 7', r.result.forecasts[1].parts.night.temp_avg === 7, JSON.stringify(r.result.forecasts[1].parts.night));
        check('OWM: завтра-утро temp = 9', r.result.forecasts[1].parts.morning.temp_avg === 9, JSON.stringify(r.result.forecasts[1].parts.morning));
        check('OWM: forecasts[1].parts завтра', r.result.forecasts[1] && r.result.forecasts[1].parts && r.result.forecasts[1].parts.night, JSON.stringify(r.result.forecasts[1] && r.result.forecasts[1].parts && r.result.forecasts[1].parts.night));
        check('OWM: иконки частей в CSS', iconsOk(r.result.forecasts[0].parts));
        check('OWM: hours sane', hoursSane(r.result.forecasts[0].hours));
    }
})();

// ============================= Open-Meteo =============================
(function () {
    const H = { time: [], weathercode: [], temperature_2m: [], relativehumidity_2m: [], surface_pressure: [], windspeed_10m: [], winddirection_10m: [] };
    const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    for (let i = 0; i < 48; i++) {
        const d = new Date(day0.getTime() + i * 3600000);
        H.time.push(tstr(d)); H.weathercode.push(i % 3);
        let t = 8 + i; // сегодня: уникальные
        if (i >= 24 && i < 30) t = 7;   // завтра 0-6ч
        else if (i >= 30 && i < 36) t = 9; // завтра 6-12ч
        H.temperature_2m.push(t);
        H.relativehumidity_2m.push(60); H.surface_pressure.push(1005);
        H.windspeed_10m.push(3.6 * (1 + i % 4)); H.winddirection_10m.push(90 + i);
    }
    const om = {
        current_weather: { temperature: 12.4, windspeed: 7.2, winddirection: 123, weathercode: 1, is_day: 1, time: tstr(new Date(Math.floor(now.getTime() / 3600000) * 3600000)) },
        hourly: H,
        daily: { sunrise: [dstr(day0) + 'T05:55'], sunset: [dstr(day0) + 'T17:55'] }
    };
    const r = runConvert('fetch_from_om_client', [
        { match: 'api.open-meteo.com/v1/forecast', payload: om }
    ]);
    check('OM: конвертация без ошибок', !r.error && r.result, r.error || (r.threw ? 'THROW:' + r.threw.message : ''));
    if (r.result) {
        check('OM: fact temp = current_weather', r.result.fact.temp === 12, JSON.stringify(r.result.fact));
        check('OM: humidity из hourly (не null при данных)', r.result.fact.humidity === 60, JSON.stringify(r.result.fact.humidity));
        check('OM: forecasts[1].parts.night завтра', r.result.forecasts[1] && r.result.forecasts[1].parts && r.result.forecasts[1].parts.night);
        check('OM: завтра-ночь temp = 7', r.result.forecasts[1].parts.night.temp_avg === 7, JSON.stringify(r.result.forecasts[1].parts.night));
        check('OM: завтра-утро temp = 9', r.result.forecasts[1].parts.morning.temp_avg === 9, JSON.stringify(r.result.forecasts[1].parts.morning));
        check('OM: иконки частей в CSS', iconsOk(r.result.forecasts[0].parts));
        check('OM: hours sane', hoursSane(r.result.forecasts[0].hours));
    }
})();

// ============================= 7timer =============================
(function () {
    const init = new Date(now.getTime() - 3 * 3600000); // init = сейчас-3ч (UTC): timepoint 3 == now
    const initStr = '' + init.getUTCFullYear() + pad2(init.getUTCMonth() + 1) + pad2(init.getUTCDate()) + pad2(init.getUTCHours());
    const ds = [];
    for (let i = 1; i <= 64; i++) {
        const tp = i * 3;
        // завтрашние диапазоны — константные температуры для однозначных ассертов
        let t = 1000 + tp;
        const ptLocal = new Date(Date.UTC(init.getUTCFullYear(), init.getUTCMonth(), init.getUTCDate(), init.getUTCHours()) + tp * 3600000);
        const tomorrow = new Date(now.getTime() + 24 * 3600000);
        const isTomorrow = ptLocal.getDate() === tomorrow.getDate() && ptLocal.getMonth() === tomorrow.getMonth();
        if (isTomorrow && ptLocal.getHours() < 6) t = 7;
        else if (isTomorrow && ptLocal.getHours() >= 6 && ptLocal.getHours() < 12) t = 9;
        ds.push({ timepoint: tp, cloudcover: i % 2 ? 20 : 80, prec_type: 'none', temp2m: t, rh2m: (40 + i % 30) + '%', wind10m: { direction: 'SE', speed: 3 } });
    }
    const r = runConvert('fetch_from_7timer_client', [
        { match: '7timer.info/bin/civil.php', payload: { dataseries: ds, init: initStr } }
    ]);
    check('7timer: конвертация без ошибок', !r.error && r.result, r.error || (r.threw ? 'THROW:' + r.threw.message : ''));
    if (r.result) {
        check('7timer: факт = блок timepoint 3 (=now), 1003 (tz-скос дал бы 1006)', r.result.fact.temp === 1003, JSON.stringify(r.result.fact.temp));
        check('7timer: humidity из rh2m', typeof r.result.fact.humidity === 'number', JSON.stringify(r.result.fact.humidity));
        check('7timer: давление честно null', r.result.fact.pressure_mm === null);
        check('7timer: forecasts[1].parts.night завтра', r.result.forecasts[1] && r.result.forecasts[1].parts && r.result.forecasts[1].parts.night);
        const nIc = r.result.forecasts[1].parts.night.icon;
        check('7timer: завтра-ночь иконка ночная (_n) или ovc*', nIc.indexOf('_n') >= 0 || nIc.indexOf('ovc') === 0, nIc);
        check('7timer: завтра-ночь temp = 7 (константа ночного бэнда)', r.result.forecasts[1].parts.night.temp_avg === 7, JSON.stringify(r.result.forecasts[1].parts.night));
        check('7timer: завтра-утро temp = 9', r.result.forecasts[1].parts.morning.temp_avg === 9, JSON.stringify(r.result.forecasts[1].parts.morning));
        check('7timer: иконки частей в CSS', iconsOk(r.result.forecasts[0].parts));
    }
})();

// ============================= wttr.in =============================
(function () {
    const day = { maxtempC: '25', mintempC: '9', astronomy: [{ sunrise: '05:55 AM', sunset: '05:55 PM' }], hourly: [] };
    for (let h = 0; h < 8; h++) {
        day.hourly.push({ time: String(h * 300), tempC: String(10 + h * 2), weatherCode: '116', windspeedKmph: '10', winddirDegree: '120', humidity: '55', pressure: '1015' });
    }
    const days = [day, JSON.parse(JSON.stringify(day)), JSON.parse(JSON.stringify(day))];
    const cc = { temp_C: '17', windspeedKmph: '12', winddirDegree: '130', humidity: '52', pressure: '1015', weatherCode: '116' };
    const r = runConvert('fetch_from_wttr', [
        { match: 'wttr.in/', payload: { current_condition: [cc], weather: days } }
    ]);
    check('wttr: конвертация без ошибок', !r.error && r.result, r.error || (r.threw ? 'THROW:' + r.threw.message : ''));
    if (r.result) {
        const p = r.result.forecasts[0].parts;
        // регресс-защита deb4b06: утро/вечер = tempC блоков 09:00/18:00, НЕ maxtempC
        const mExp = 10 + 3 * 2, eExp = 10 + 6 * 2;
        check('wttr: утро = блок 09:00 (' + mExp + ')', p.morning.temp_avg === mExp, JSON.stringify(p.morning));
        check('wttr: вечер = блок 18:00 (' + eExp + ')', p.evening.temp_avg === eExp, JSON.stringify(p.evening));
        check('wttr: день = maxtempC', p.day.temp_avg === 25, JSON.stringify(p.day));
        check('wttr: иконки частей в CSS', iconsOk(p));
    }
})();

// ============================= Foreca =============================
(function () {
    const fc = [];
    const tmrF = new Date(now.getTime() + 24 * 3600000);
    for (let i = 0; i < 49; i++) {
        const d = new Date(Math.floor(now.getTime() / 3600000) * 3600000 + i * 3600000);
        let temp = 12 + i;
        if (d.getDate() === tmrF.getDate() && d.getMonth() === tmrF.getMonth()) {
            if (d.getHours() < 6) temp = 7;
            else if (d.getHours() >= 6 && d.getHours() < 12) temp = 9;
        }
        fc.push({ time: tstr(d) + '+03:00', temperature: temp, windSpeed: 3, windDir: 180, precipProb: 0, precipAccum: 0, symbol: (d.getHours() >= 6 && d.getHours() < 20 ? 'd' : 'n') + '200' });
    }
    const r = runConvert('fetch_from_foreca_client', [
        { match: '/location/search/', payload: { locations: [{ id: 100520555, name: 'Nizhny Novgorod', timezone: 'Europe/Moscow' }] } },
        { match: '/forecast/hourly/', payload: { forecast: fc } }
    ]);
    check('Foreca: конвертация без ошибок (TDZ-регрессия 6e6343f)', !r.error && r.result, r.error || (r.threw ? 'THROW:' + r.threw.message : ''));
    if (r.result) {
        check('Foreca: humidity/pressure null (API не отдаёт)', r.result.fact.humidity === null && r.result.fact.pressure_mm === null);
        check('Foreca: forecasts[1].parts.night завтра', r.result.forecasts[1] && r.result.forecasts[1].parts && r.result.forecasts[1].parts.night);
        check('Foreca: завтра-ночь temp = 7', r.result.forecasts[1].parts.night.temp_avg === 7, JSON.stringify(r.result.forecasts[1].parts.night));
        check('Foreca: завтра-утро temp = 9', r.result.forecasts[1].parts.morning.temp_avg === 9, JSON.stringify(r.result.forecasts[1].parts.morning));
        const nIc = r.result.forecasts[1].parts.night.icon;
        check('Foreca: завтра-ночь иконка ночная/ovc', nIc.indexOf('_n') >= 0 || nIc.indexOf('ovc') === 0, nIc);
        check('Foreca: hours sane', hoursSane(r.result.forecasts[0].hours));
        check('Foreca: иконки частей в CSS', iconsOk(r.result.forecasts[0].parts));
    }
})();

// --- astro_sun: астрономический восход/закат (фолбэк для источников без солнца) ---
(function () {
    const m = html.match(/^function astro_sun[\s\S]*?^\}/m);
    check('astro_sun извлечена', !!m);
    if (m) {
        const fn = eval('(function(){' + m[0] + '; return astro_sun;})()');
        const r = fn(56.3177, 43.9993, new Date());
        check('astro_sun: НН восход в 03-08ч', r.sunrise && parseInt(r.sunrise) >= 3 && parseInt(r.sunrise) <= 8, r.sunrise);
        check('astro_sun: НН закат в 15-21ч', r.sunset && parseInt(r.sunset) >= 15 && parseInt(r.sunset) <= 21, r.sunset);
        const dur = (parseInt(r.sunset) - parseInt(r.sunrise)) * 60 + (parseInt(r.sunset.slice(3)) - parseInt(r.sunrise.slice(3)));
        check('astro_sun: длительность 10-14ч (сентябрь НН)', dur >= 600 && dur <= 840, dur + ' мин');
    }
})();

console.log('\nИТОГ: pass=' + pass + ' fail=' + fail);
process.exit(fail ? 1 : 0);
