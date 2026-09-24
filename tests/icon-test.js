#!/usr/bin/env node
// Тест значков информера: (1) все варианты _d/_n существуют в CSS,
// (2) icon_daynight() правильно переключает день/ночь по времени,
// (3) все имена значков, которые может выдать сервер, покрыты CSS.
// Запуск: node tests/icon-test.js  (без зависимостей)
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'informer.html'), 'utf8');

let pass = 0, fail = 0;
const problems = [];
function check(name, cond, detail) {
    if (cond) { pass++; console.log('  [OK]  ' + name); }
    else { fail++; console.log('  [FAIL] ' + name + (detail ? ' — ' + detail : '')); problems.push(name); }
}

// --- 1. Все значковые CSS-классы из файла (те, что с svg-фоном) ---
const cssClasses = new Set();
for (const m of html.matchAll(/\.([A-Za-z0-9_+\-]+)\s*\{[^}]*?data:image\/svg\+xml/g)) cssClasses.add(m[1]);
console.log('CSS-классов значков: ' + cssClasses.size);
check('есть skc_d (солнце)', cssClasses.has('skc_d'));
check('есть skc_n (полумесяц)', cssClasses.has('skc_n'));
check('есть bkn_d/bkn_n (перем. облачность)', cssClasses.has('bkn_d') && cssClasses.has('bkn_n'));
check('есть ovc (пасмурно, без сут. варианта)', cssClasses.has('ovc'));

// --- 2. Покрытие _d/_n пар: у каждого кода с _d должна быть пара _n и наоборот ---
const dayOnly = [], nightOnly = [];
for (const c of cssClasses) {
    if (c.endsWith('_d') && !cssClasses.has(c.slice(0, -2) + '_n')) dayOnly.push(c);
    if (c.endsWith('_n') && !cssClasses.has(c.slice(0, -2) + '_d')) nightOnly.push(c);
}
check('парность _d/_n (нет одиночек, кроме легитимных)', dayOnly.length === 0 && nightOnly.length === 0,
      dayOnly.concat(nightOnly).join(','));

// --- 3. icon_daynight: поведение по времени суток (мок часов) ---
// вырезаем функцию из html и исполняем с подменёнными Date.getHours/getMinutes
const fnSrc = (html.match(/function icon_daynight[\s\S]*?\n\}/) || [])[0];
check('icon_daynight присутствует', !!fnSrc);

function withClock(h, m, fn) {
    const RealDate = Date;
    const Fixed = class extends RealDate {
        constructor(...a) { if (a.length === 0) super(2026, 8, 24, h, m, 0); else super(...a); }
        static now() { return new Fixed(2026, 8, 24, h, m, 0).getTime(); }
    };
    const RealGetH = Date.prototype.getHours, RealGetM = Date.prototype.getMinutes;
    Date.prototype.getHours = function () { return this instanceof Fixed ? RealGetH.call(this) : h; };
    Date.prototype.getMinutes = function () { return this instanceof Fixed ? RealGetM.call(this) : m; };
    global.Date = Fixed;
    try { fn(); } finally {
        global.Date = RealDate;
        Date.prototype.getHours = RealGetH; Date.prototype.getMinutes = RealGetM;
    }
}

const cases = [
    // [время, вход, ожидание] — закат 17:59, восход 05:52
    [12, 0,  'skc_d', 'skc_d'],   // ясный день — солнце
    [18, 34, 'skc_d', 'skc_n'],   // после заката — полумесяц (наш баг)
    [23, 59, 'skc_d', 'skc_n'],   // глубокая ночь
    [0,  30, 'skc_d', 'skc_n'],   // после полуночи
    [5,  51, 'bkn_d', 'bkn_n'],   // за минуту до восхода
    [5,  53, 'bkn_n', 'bkn_d'],   // после восхода — обратно день
    [17, 58, 'bkn_n', 'bkn_d'],   // до заката — ночной вариант днём не остаётся
    [11, 0,  'bkn_n', 'bkn_d'],
    // без суффикса — не трогаем
    [18, 34, 'ovc',   'ovc'],
    // нет восхода/заката — фолбэк 08:00-20:00
    [19, 0,  'skc_d', 'skc_n'],
    [21, 0,  'skc_d', 'skc_n'],
    // мусор/пусто — не падаем
    [12, 0,  '',      ''],
    [12, 0,  null,    null],
];

if (fnSrc) {
    for (const [h, m, input, expected] of cases) {
        withClock(h, m, () => {
            const fn = new Function('return (' + fnSrc + ')')();
            const got = fn(input, '05:52', '17:59');
            check(`icon_daynight ${JSON.stringify(input)} @ ${h}:${String(m).padStart(2, '0')} → ${expected}`,
                  got === expected, 'получено ' + got);
        });
    }
}

// --- 4. Вокабуляр сервера покрыт CSS ---
// эмуляция конвертеров OM/wttr/7timer: суффикс _d/_n только для skc/bkn (как в патче сервера)
const suffix = (b, isDay) => (b === 'skc' || b === 'bkn') ? b + (isDay ? '_d' : '_n') : b;
const OM = { 0:'skc', 1:'bkn', 2:'bkn', 3:'ovc', 45:'ovc', 48:'ovc', 51:'ovc_ra', 53:'ovc_ra', 55:'ovc_ra', 61:'ovc_ra', 63:'ovc_ra', 65:'ovc_ra', 80:'ovc_ra', 81:'ovc_ra', 82:'ovc_ra', 71:'ovc_sn', 73:'ovc_sn', 75:'ovc_sn', 85:'ovc_sn', 86:'ovc_sn', 95:'ovc_ts', 96:'ovc_ts', 99:'ovc_ts' };
const WWO = { 113:'skc', 116:'bkn', 119:'ovc', 122:'ovc', 143:'ovc', 176:'ovc_ra', 179:'ovc_sn', 182:'ovc_ra', 185:'ovc_sn', 200:'ovc_ts', 227:'ovc_sn', 230:'ovc_sn', 248:'ovc', 260:'ovc', 263:'ovc_ra', 281:'ovc_ra', 293:'ovc_ra', 296:'ovc_ra', 299:'ovc_ra', 302:'ovc_ra', 305:'ovc_ra', 308:'ovc_ra', 320:'ovc_sn', 323:'ovc_sn', 326:'ovc_sn', 329:'ovc_sn', 332:'ovc_sn', 335:'ovc_sn', 338:'ovc_sn', 350:'ovc_ra', 353:'ovc_ra', 356:'ovc_ra', 359:'ovc_ra', 362:'ovc_ra', 365:'ovc_ra', 367:'ovc_ra', 368:'ovc_sn', 371:'ovc_ra', 374:'ovc_ra', 377:'ovc_ra', 386:'ovc_ts', 389:'ovc_ts' };
const emitted = new Set();
for (const b of Object.values(OM)) { emitted.add(suffix(b, true)); emitted.add(suffix(b, false)); }
for (const b of Object.values(WWO)) { emitted.add(suffix(b, true)); emitted.add(suffix(b, false)); }
// OWM (OpenWeatherMap): id-группы -> имена
const OWM = {};
for (let id = 200; id < 300; id++) OWM[id] = 'ovc_ts';
for (let id = 300; id < 400; id++) OWM[id] = 'ovc_ra';
for (let id = 500; id < 600; id++) OWM[id] = 'ovc_ra';
for (let id = 600; id < 700; id++) OWM[id] = 'ovc_sn';
for (let id = 700; id < 800; id++) OWM[id] = 'ovc';
OWM[800] = 'skc'; OWM[801] = 'bkn'; OWM[802] = 'bkn'; OWM[803] = 'ovc'; OWM[804] = 'ovc';
let vocFailOwm = 0;
for (const k of Object.keys(OWM)) {
    const b = OWM[k];
    const emit = (b === 'skc' || b === 'bkn') ? [b + '_d', b + '_n'] : [b];
    for (const ic of emit) if (!cssClasses.has(ic)) { vocFailOwm++; console.log('  [FAIL] OWM id ' + k + ' -> нет CSS для "' + ic + '"'); }
}
check('OWM-вокабуляр покрыт CSS', vocFailOwm === 0, vocFailOwm + ' отсутствуют');
console.log('OWM id-групп: ' + Object.keys(OWM).length);
console.log('имён значков, порождаемых сервером: ' + emitted.size);
let vocFail = 0;
for (const ic of emitted) if (!cssClasses.has(ic)) { vocFail++; console.log('  [FAIL] нет CSS для "' + ic + '"'); }
check('весь серверный вокабуляр значков покрыт CSS', vocFail === 0, vocFail + ' отсутствуют');
console.log('\nИТОГ: pass=' + pass + ' fail=' + fail);
process.exit(fail ? 1 : 0);
