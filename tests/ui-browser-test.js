#!/usr/bin/env node
/**
 * ui-browser-test.js — E2E UI & Layout verification in real headless Chromium.
 * Runs with Chromium via Chrome DevTools Protocol (CDP) with zero external npm dependencies.
 *
 * Verifies:
 * 1. CSSOM integrity: stylesheet is fully parsed, no unclosed quotes/syntax bugs, all @media queries present.
 * 2. Multi-resolution layout invariants: 1024x600 (Tablets 1, 2, 4) and 1280x800 (Tablet 3).
 *    Strict check: scrollHeight <= clientHeight (NO vertical/horizontal page scrollbars/overflow).
 * 3. Dynamic Weather Modes:
 *    - Calm wind (0 m/s) -> class clear, progress 0%
 *    - Light wind (3 m/s) -> class clear, fill ~20%
 *    - Fresh wind (7 m/s) -> .wind_fresh, #ffd600 color
 *    - Strong wind (12 m/s) -> .wind_strong, #ff9100 color
 *    - Storm wind (18 m/s) -> .wind_storm, #ff1744 color
 *    - Pressure trend with elapsed time: ↑+7 (12ч), ↓-4 (6ч), stable (hidden)
 * 4. Captures visual verification screenshots.
 */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROMIUM_BIN = '/snap/bin/chromium';
const HTML_PATH = path.resolve(__dirname, '../informer.html');
const SETTINGS_PATH = path.resolve(__dirname, '../settings.html');
const SETUP_PATH = path.resolve(__dirname, '../setup.html');
const CDP_PORT = 9240;

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) {
        pass++;
        console.log('  [OK] ' + name);
    } else {
        fail++;
        console.log('  [FAIL] ' + name + (extra ? ' — ' + extra : ''));
    }
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

class CDPClient {
    constructor(wsUrl) {
        this.wsUrl = wsUrl;
        this.ws = null;
        this.msgId = 1;
        this.pending = new Map();
    }

    async connect() {
        this.ws = new WebSocket(this.wsUrl);
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.id && this.pending.has(data.id)) {
                this.pending.get(data.id)(data);
                this.pending.delete(data.id);
            }
        };
        await new Promise((resolve, reject) => {
            this.ws.onopen = resolve;
            this.ws.onerror = reject;
        });
    }

    send(method, params = {}) {
        return new Promise((resolve) => {
            const id = this.msgId++;
            this.pending.set(id, resolve);
            this.ws.send(JSON.stringify({ id, method, params }));
        });
    }

    async evaluate(expression) {
        const res = await this.send('Runtime.evaluate', {
            expression,
            awaitPromise: true,
            returnByValue: true
        });
        if (res.result && res.result.exceptionDetails) {
            throw new Error(JSON.stringify(res.result.exceptionDetails));
        }
        return res.result ? res.result.result.value : undefined;
    }

    close() {
        if (this.ws) {
            this.ws.close();
        }
    }
}

async function main() {
    console.log('--- UI Headless Chromium Test Suite ---');

    if (!fs.existsSync(CHROMIUM_BIN)) {
        console.error('Chromium binary not found at ' + CHROMIUM_BIN);
        process.exit(1);
    }

    const chrome = spawn(CHROMIUM_BIN, [
        '--headless=new',
        `--remote-debugging-port=${CDP_PORT}`,
        '--no-sandbox',
        '--disable-gpu',
        '--window-size=1024,600'
    ]);

    let cdp = null;

    try {
        let versionRes = null;
        for (let i = 0; i < 25; i++) {
            await delay(200);
            try {
                const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
                if (res.ok) { versionRes = res; break; }
            } catch (e) {}
        }
        if (!versionRes) throw new Error("Could not connect to Chromium on port " + CDP_PORT);

        const version = await versionRes.json();
        console.log('Browser:', version.Browser);

        const newTabRes = await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?file://${HTML_PATH}`, { method: 'PUT' });
        const tab = await newTabRes.json();

        cdp = new CDPClient(tab.webSocketDebuggerUrl);
        await cdp.connect();

        await cdp.send('Page.enable');
        await cdp.send('Runtime.enable');
        await delay(1500);

        // ==========================================
        // 1. CSSOM Integrity & Syntax Verification
        // ==========================================
        console.log('\n[1] CSSOM Integrity & Syntax:');
        const cssom = await cdp.evaluate(`(() => {
            const sheets = Array.from(document.styleSheets);
            const mainSheet = sheets[1];
            if (!mainSheet) return { error: 'main sheet missing' };

            const rules = Array.from(mainSheet.cssRules);
            const landscapeRule = rules.find(r => r.media && r.media.mediaText.includes('landscape'));
            const portraitRule = rules.find(r => r.media && r.media.mediaText.includes('portrait'));
            const findMedia = (text) => {
                for (const r of rules) {
                    if (r.media && r.media.mediaText.includes(text)) return true;
                    if (r.cssRules) {
                        for (const inner of r.cssRules) {
                            if (inner.media && inner.media.mediaText.includes(text)) return true;
                        }
                    }
                }
                return false;
            };
            const hasRatioMedia = findMedia('168/100') || findMedia('1280px');

            // Check if any url(...) contains unclosed quotes or syntax errors
            const cssTexts = rules.map(r => r.cssText).join('\\n');
            const brokenUrlQuote = /url\\(["\'][^"\\n]*\\s+!important/.test(cssTexts);

            return {
                totalRules: rules.length,
                hasLandscapeMedia: !!landscapeRule,
                hasPortraitMedia: !!portraitRule,
                hasRatioMedia: !!hasRatioMedia,
                brokenUrlQuote
            };
        })()`);

        check('CSSOM: main stylesheet parsed completely (>=115 rules)', cssom.totalRules >= 115, 'actual: ' + cssom.totalRules);
        check('CSSOM: @media (orientation: landscape) present', cssom.hasLandscapeMedia);
        check('CSSOM: @media (orientation: portrait) present', cssom.hasPortraitMedia);
        check('CSSOM: @media tablet aspect-ratio present', cssom.hasRatioMedia);
        check('CSSOM: no unclosed url() quotes swallowed rules', !cssom.brokenUrlQuote);

        // ==========================================
        // 2. Viewport Invariants: 1024x600 (Tablets 1, 2, 4)
        // ==========================================
        console.log('\n[2] Tablet Layout 1024x600 (Landscape):');
        await cdp.send('Emulation.setDeviceMetricsOverride', {
            width: 1024,
            height: 600,
            deviceScaleFactor: 1,
            mobile: false
        });
        await delay(500);

        const layout1024 = await cdp.evaluate(`(() => {
            const wrapper = document.getElementById('wrapper');
            const weatherBody = document.getElementById('weather_body');
            const now = document.getElementById('now');
            const right = document.getElementById('weather_right');
            const whp = document.getElementById('wind_humidity_pressure');

            return {
                scrollWidth: document.body.scrollWidth,
                scrollHeight: document.body.scrollHeight,
                clientWidth: document.documentElement.clientWidth,
                clientHeight: document.documentElement.clientHeight,
                wrapperDisplay: window.getComputedStyle(wrapper).display,
                wrapperHeight: wrapper.offsetHeight,
                weatherBodyDisplay: window.getComputedStyle(weatherBody).display,
                nowWidth: now.offsetWidth,
                rightWidth: right.offsetWidth,
                whpDisplay: window.getComputedStyle(whp).display
            };
        })()`);

        check('1024x600: zero vertical scrollbar (scrollHeight <= clientHeight)', layout1024.scrollHeight <= layout1024.clientHeight,
            `scrollHeight=${layout1024.scrollHeight}, clientHeight=${layout1024.clientHeight}`);
        check('1024x600: zero horizontal scrollbar (scrollWidth <= clientWidth)', layout1024.scrollWidth <= layout1024.clientWidth,
            `scrollWidth=${layout1024.scrollWidth}, clientWidth=${layout1024.clientWidth}`);
        check('1024x600: #wrapper is flex column filling 600px', layout1024.wrapperDisplay === 'flex' && layout1024.wrapperHeight === 600);
        check('1024x600: #now and #weather_right split columns (512px each)', layout1024.nowWidth === 512 && layout1024.rightWidth === 512);

        // ==========================================
        // 3. Viewport Invariants: 1280x800 (Tablet 3)
        // ==========================================
        console.log('\n[3] Tablet Layout 1280x800 (Landscape):');
        await cdp.send('Emulation.setDeviceMetricsOverride', {
            width: 1280,
            height: 800,
            deviceScaleFactor: 1,
            mobile: false
        });
        await delay(500);

        const layout1280 = await cdp.evaluate(`(() => {
            return {
                scrollWidth: document.body.scrollWidth,
                scrollHeight: document.body.scrollHeight,
                clientWidth: document.documentElement.clientWidth,
                clientHeight: document.documentElement.clientHeight,
                wrapperHeight: document.getElementById('wrapper').offsetHeight,
                nowWidth: document.getElementById('now').offsetWidth,
                rightWidth: document.getElementById('weather_right').offsetWidth
            };
        })()`);

        check('1280x800: zero vertical scrollbar (scrollHeight <= clientHeight)', layout1280.scrollHeight <= layout1280.clientHeight,
            `scrollHeight=${layout1280.scrollHeight}, clientHeight=${layout1280.clientHeight}`);
        check('1280x800: zero horizontal scrollbar (scrollWidth <= clientWidth)', layout1280.scrollWidth <= layout1280.clientWidth,
            `scrollWidth=${layout1280.scrollWidth}, clientWidth=${layout1280.clientWidth}`);
        check('1280x800: #now and #weather_right split columns (640px each)', layout1280.nowWidth === 640 && layout1280.rightWidth === 640);

        // ==========================================
        // 4. Dynamic Weather Modes Testing
        // ==========================================
        console.log('\n[4] Weather Modes (Wind & Pressure Dynamics):');

        // Helper to inject mock weather
        async function renderMock(factPatch, pressureHistory) {
            return await cdp.evaluate(`(async () => {
                const baseData = {
                    now: Date.now() / 1000,
                    fact: Object.assign({
                        temp: 15,
                        feels_like: 14,
                        condition: 'partly-cloudy',
                        icon: 'bkn_d',
                        wind_speed: 3,
                        wind_angle: 90,
                        humidity: 60,
                        pressure_mm: 745
                    }, ${JSON.stringify(factPatch)}),
                    forecasts: [{
                        date: new Date().toISOString().slice(0, 10),
                        sunrise: '06:30',
                        sunset: '18:30',
                        moon_code: 1,
                        hours: [{ hour: 12, temp: 15 }],
                        parts: {
                            day: { temp_avg: 16, wind_speed: 4, icon: 'bkn_d' },
                            evening: { temp_avg: 12, wind_speed: 2, icon: 'bkn_n' }
                        }
                    }]
                };
                if (${JSON.stringify(pressureHistory)}) {
                    localStorage.setItem('weather_pressure_history', JSON.stringify(${JSON.stringify(pressureHistory)}));
                }
                render_weather(baseData);
                // Wait for CSS 0.3s transition to complete
                await new Promise(r => setTimeout(r, 350));

                const sectorWind = document.querySelector('.sector_wind');
                const windText = document.getElementById('wind').innerText.trim();
                const windProg = document.getElementById('wind_progress');
                const windIcon = document.getElementById('wind_icon');
                const pressTrend = document.getElementById('pressure_trend');
                const pressVal = document.getElementById('pressure').innerText.trim();
                const pressProg = document.getElementById('pressure_progress');
                const fc0 = document.querySelector('.sector_forecast_wind_0');
                const fc1 = document.querySelector('.sector_forecast_wind_1');
                const now = document.getElementById('now');
                const pt = document.getElementById('pressure_trend');

                return {
                    sectorWindClass: sectorWind ? sectorWind.className : '',
                    windText,
                    windProgStyle: windProg ? windProg.getAttribute('style') : '',
                    windProgBg: windProg ? window.getComputedStyle(windProg).backgroundColor : '',
                    windProgWidth: windProg ? windProg.style.width : '',
                    pressTrendHtml: pressTrend ? pressTrend.innerHTML : '',
                    pressTrendVisible: pressTrend ? window.getComputedStyle(pressTrend).display !== 'none' : false,
                    pressVal,
                    pressProgStyle: pressProg ? pressProg.getAttribute('style') : '',
                    fc0Class: fc0 ? fc0.className : '',
                    fc1Class: fc1 ? fc1.className : '',
                    fc0Color: fc0 ? window.getComputedStyle(fc0.querySelector('span')).color : '',
                    fc1Color: fc1 ? window.getComputedStyle(fc1.querySelector('span')).color : '',
                    ptRightOverflow: (pt && now) ? Math.round(pt.getBoundingClientRect().right - now.getBoundingClientRect().right) : 0
                };
            })()`);
        }

        // Mode 1: Calm wind (0 m/s)
        const mCalm = await renderMock({ wind_speed: 0 });
        check('Mode 1: штиль (0 м/с) → нет спецклассов ветра', !mCalm.sectorWindClass.includes('wind_'));
        check('Mode 1: штиль (0 м/с) → прогресс 0%', mCalm.windProgWidth === '0%');

        // Mode 2: Light wind (3 m/s)
        const mLight = await renderMock({ wind_speed: 3 });
        check('Mode 2: легкий ветер (3 м/с) → нет спецклассов ветра', !mLight.sectorWindClass.includes('wind_'));
        check('Mode 2: легкий ветер (3 м/с) → зеленый прогресс (width ~20%)',
            mLight.windProgBg === 'rgb(0, 230, 118)' && mLight.windProgWidth === '20%');

        // Mode 3: Fresh wind (7 m/s)
        const mFresh = await renderMock({ wind_speed: 7 });
        check('Mode 3: свежий ветер (7 м/с) → класс wind_fresh', mFresh.sectorWindClass.includes('wind_fresh'));
        check('Mode 3: свежий ветер (7 м/с) → желтый цвет (#ffd600)',
            mFresh.windProgBg === 'rgb(255, 214, 0)' && mFresh.windProgStyle.includes('#ffd600'));

        // Mode 4: Strong wind (12 m/s)
        const mStrong = await renderMock({ wind_speed: 12 });
        check('Mode 4: сильный ветер (12 м/с) → класс wind_strong', mStrong.sectorWindClass.includes('wind_strong'));
        check('Mode 4: сильный ветер (12 м/с) → оранжевый цвет (#ff9100)',
            mStrong.windProgBg === 'rgb(255, 145, 0)' && mStrong.windProgStyle.includes('#ff9100'));

        // Mode 5: Storm wind (18 m/s)
        const mStorm = await renderMock({ wind_speed: 18 });
        check('Mode 5: шторм (18 м/с) → класс wind_storm', mStorm.sectorWindClass.includes('wind_storm'));
        check('Mode 5: шторм (18 м/с) → красный цвет (#ff1744)',
            mStorm.windProgBg === 'rgb(255, 23, 68)' && mStorm.windProgStyle.includes('#ff1744'));

        // Mode 6: Pressure trend with time brackets
        const nowMs = Date.now();
        const history12h = [
            { t: nowMs - 12 * 3600 * 1000, p: 738 }
        ];
        const mTrendUp = await renderMock({ pressure_mm: 745 }, history12h);
        check('Mode 6a: тренд давления рост (+7) → виден', mTrendUp.pressTrendVisible);
        check('Mode 6a: тренд давления рост (+7) → содержит стрелку ↑ и +7',
            mTrendUp.pressTrendHtml.includes('↑') && mTrendUp.pressTrendHtml.includes('+7'));
        check('Mode 6a: тренд давления рост (+7) → содержит интервал (12ч)',
            mTrendUp.pressTrendHtml.includes('(12ч)'));
        check('Mode 6a: строка давления с интервалом (12ч) полностью внутри границ (overflow <= 0px)',
            mTrendUp.ptRightOverflow <= 0, `overflow=${mTrendUp.ptRightOverflow}px`);

        const history4h = [
            { t: nowMs - 4 * 3600 * 1000, p: 748 }
        ];
        const mTrendDown = await renderMock({ pressure_mm: 745 }, history4h);
        check('Mode 6b: тренд давления падение (-3) → содержит стрелку ↓ и -3',
            mTrendDown.pressTrendHtml.includes('↓') && mTrendDown.pressTrendHtml.includes('-3'));
        check('Mode 6b: тренд давления падение (-3) → содержит интервал (4ч)',
            mTrendDown.pressTrendHtml.includes('(4ч)'));
        check('Mode 6b: строка давления с интервалом (4ч) полностью внутри границ',
            mTrendDown.ptRightOverflow <= 0);

        // Mode 7: Forecast slots wind indication (evening & night)
        const mForecastWind = await cdp.evaluate(`(async () => {
            const partsToday = {
                morning: { temp_avg: 10, wind_speed: 7 },
                day: { temp_avg: 16, wind_speed: 7 },
                evening: { temp_avg: 12, wind_speed: 7 },
                night: { temp_avg: 8, wind_speed: 12 }
            };
            const partsTomorrow = {
                morning: { temp_avg: 9, wind_speed: 12 },
                day: { temp_avg: 15, wind_speed: 12 },
                evening: { temp_avg: 11, wind_speed: 12 },
                night: { temp_avg: 7, wind_speed: 12 }
            };
            const origGetHours = Date.prototype.getHours;
            Date.prototype.getHours = function() { return 14; };
            try {
                render_weather({
                    fact: { temp: 15, wind_speed: 3, humidity: 60, pressure_mm: 745 },
                    forecasts: [{ parts: partsToday }, { parts: partsTomorrow }]
                });
            } finally {
                Date.prototype.getHours = origGetHours;
            }
            await new Promise(r => setTimeout(r, 350));
            const fc0 = document.querySelector('.sector_forecast_wind_0');
            const fc1 = document.querySelector('.sector_forecast_wind_1');
            const fc0Bar = document.getElementById('forecast_wind_progress_0');
            const fc1Bar = document.getElementById('forecast_wind_progress_1');
            return {
                fc0Class: fc0 ? fc0.className : '',
                fc1Class: fc1 ? fc1.className : '',
                fc0Color: fc0 ? window.getComputedStyle(fc0.querySelector('span')).color : '',
                fc1Color: fc1 ? window.getComputedStyle(fc1.querySelector('span')).color : '',
                fc0BarWidth: fc0Bar ? fc0Bar.style.width : '',
                fc0BarBg: fc0Bar ? fc0Bar.style.backgroundColor : '',
                fc1BarWidth: fc1Bar ? fc1Bar.style.width : '',
                fc1BarBg: fc1Bar ? fc1Bar.style.backgroundColor : ''
            };
        })()`);

        check('Mode 7a: прогноз слот 0 (7 м/с) → класс wind_fresh (#ffd600)',
            mForecastWind.fc0Class.includes('wind_fresh') && mForecastWind.fc0Color === 'rgb(255, 214, 0)');
        check('Mode 7b: прогноз слот 1 (12 м/с) → класс wind_strong (#ff9100)',
            mForecastWind.fc1Class.includes('wind_strong') && mForecastWind.fc1Color === 'rgb(255, 145, 0)');
        check('Mode 7c: прогноз мини-прогресс бар слот 0 → 47%, желтый',
            mForecastWind.fc0BarWidth === '47%' && (mForecastWind.fc0BarBg === 'rgb(255, 214, 0)' || mForecastWind.fc0BarBg === '#ffd600'));
        check('Mode 7d: прогноз мини-прогресс бар слот 1 → 80%, оранжевый',
            mForecastWind.fc1BarWidth === '80%' && (mForecastWind.fc1BarBg === 'rgb(255, 145, 0)' || mForecastWind.fc1BarBg === '#ff9100'));

        // Mode 8: Wind Progress Bar Scale & Track Markers
        const windBarMarks = await cdp.evaluate(`(() => {
            const bar = document.querySelector('.wind_pressure_bar');
            const mark5 = bar ? bar.querySelector('.wind_mark_5') : null;
            const mark10 = bar ? bar.querySelector('.wind_mark_10') : null;
            const comp = bar ? window.getComputedStyle(bar) : null;
            const bRect = bar ? bar.getBoundingClientRect() : null;
            const m5Rect = mark5 ? mark5.getBoundingClientRect() : null;
            const m10Rect = mark10 ? mark10.getBoundingClientRect() : null;
            return {
                hasMark5: !!mark5,
                hasMark10: !!mark10,
                mark5Pct: (bRect && m5Rect) ? Math.round((m5Rect.left - bRect.left) / bRect.width * 100) : 0,
                mark10Pct: (bRect && m10Rect) ? Math.round((m10Rect.left - bRect.left) / bRect.width * 100) : 0,
                hasTrackShadow: comp ? comp.boxShadow.includes('rgba') : false,
                bg: comp ? comp.backgroundColor : ''
            };
        })()`);
        check('Mode 8a: шкала ветра имеет засечку свежего ветра 5 м/с (~33%)',
            windBarMarks.hasMark5 && Math.abs(windBarMarks.mark5Pct - 33) <= 2);
        check('Mode 8b: шкала ветра имеет засечку сильного ветра 10 м/с (~67%)',
            windBarMarks.hasMark10 && Math.abs(windBarMarks.mark10Pct - 67) <= 2);
        check('Mode 8c: трек шкалы имеет четкий контур box-shadow для контраста на TN экранах',
            windBarMarks.hasTrackShadow);
        check('Mode 8d: контрастный фон трека #3e424c (rgb(62, 66, 76))',
            windBarMarks.bg === 'rgb(62, 66, 76)');

        // Track styling invariant: no border, clean background
        const trackStyle = await cdp.evaluate(`(() => {
            const bar = document.querySelector('.wind_humidity_pressure_bar');
            const comp = window.getComputedStyle(bar);
            return {
                borderTopWidth: comp.borderTopWidth
            };
        })()`);
        check('Progress track has no layout-shifting borders (0px)',
            trackStyle.borderTopWidth === '0px');

        // Mode 9: Humidity Out-of-bounds warning (high humidity сырость and dry air)
        const mHumidity = await cdp.evaluate(`(async () => {
            const parts = {
                morning: { temp_avg: 10, wind_speed: 2 },
                day: { temp_avg: 15, wind_speed: 2 },
                evening: { temp_avg: 12, wind_speed: 2 },
                night: { temp_avg: 8, wind_speed: 2 }
            };
            // 9a: High humidity 80% (>65% сырость)
            render_weather({
                fact: { temp: 10, wind_speed: 2, humidity: 80, pressure_mm: 745 },
                forecasts: [{ parts: parts }, { parts: parts }]
            });
            await new Promise(r => setTimeout(r, 200));
            const secWet = document.querySelector('.sector_humidity');
            const valWet = document.getElementById('humidity');
            const barWet = document.getElementById('humidity_progress');
            const resWet = {
                wetClass: secWet ? secWet.className : '',
                wetColor: valWet ? window.getComputedStyle(valWet).color : '',
                wetBarBg: barWet ? barWet.style.backgroundColor : ''
            };

            // 9b: Dry humidity 25% (<35% сухость)
            render_weather({
                fact: { temp: 10, wind_speed: 2, humidity: 25, pressure_mm: 745 },
                forecasts: [{ parts: parts }, { parts: parts }]
            });
            await new Promise(r => setTimeout(r, 200));
            const secDry = document.querySelector('.sector_humidity');
            const valDry = document.getElementById('humidity');
            const barDry = document.getElementById('humidity_progress');
            const resDry = {
                dryClass: secDry ? secDry.className : '',
                dryColor: valDry ? window.getComputedStyle(valDry).color : '',
                dryBarBg: barDry ? barDry.style.backgroundColor : ''
            };

            // 9c: Normal comfort 50%
            render_weather({
                fact: { temp: 10, wind_speed: 2, humidity: 50, pressure_mm: 745 },
                forecasts: [{ parts: parts }, { parts: parts }]
            });
            await new Promise(r => setTimeout(r, 200));
            const secNorm = document.querySelector('.sector_humidity');
            const barNorm = document.getElementById('humidity_progress');
            const resNorm = {
                normClass: secNorm ? secNorm.className : '',
                normBarBg: barNorm ? barNorm.style.backgroundColor : ''
            };

            return {
                wetClass: resWet.wetClass,
                wetColor: resWet.wetColor,
                wetBarBg: resWet.wetBarBg,
                dryClass: resDry.dryClass,
                dryColor: resDry.dryColor,
                dryBarBg: resDry.dryBarBg,
                normClass: resNorm.normClass,
                normBarBg: resNorm.normBarBg
            };
        })()`);

        check('Mode 9a: влажность 80% (сырость >65%) → класс humidity_wet, цвет #ab47bc',
            mHumidity.wetClass.includes('humidity_wet') && mHumidity.wetColor === 'rgb(171, 71, 188)' && (mHumidity.wetBarBg === 'rgb(171, 71, 188)' || mHumidity.wetBarBg === '#ab47bc'));
        check('Mode 9b: влажность 25% (сухость <35%) → класс humidity_dry, цвет #ff9100',
            mHumidity.dryClass.includes('humidity_dry') && mHumidity.dryColor === 'rgb(255, 145, 0)' && (mHumidity.dryBarBg === 'rgb(255, 145, 0)' || mHumidity.dryBarBg === '#ff9100'));
        check('Mode 9c: норма влажности 50% → без классов warning, полоса #00e676',
            !mHumidity.normClass.includes('humidity_dry') && !mHumidity.normClass.includes('humidity_wet') && (mHumidity.normBarBg === 'rgb(0, 230, 118)' || mHumidity.normBarBg === '#00e676'));

        // ==========================================
        // 5. QR Onboarding & Dual/Single Code Mode
        // ==========================================
        console.log('\n[5] QR-code Onboarding & Dual/Single Code Mode:');
        // 5a: Standalone mode (server absent)
        const qrStandalone = await cdp.evaluate(`(async () => {
            const origAjax = $.ajax;
            $.ajax = function(opts) {
                const d = $.Deferred();
                setTimeout(() => {
                    d.resolve({ status: "ok", device_ip: "192.168.1.50", server_url: "" });
                }, 10);
                return d.promise();
            };
            show_qr_onboarding();
            await new Promise(r => setTimeout(r, 200));
            $.ajax = origAjax;
            const modal = document.getElementById('qr_modal');
            const cardTablet = document.getElementById('card_qr_tablet');
            const cardServer = document.getElementById('card_qr_server');
            const titleTablet = document.getElementById('title_qr_tablet');
            const urlTablet = document.getElementById('url_qr_tablet');
            const qrTabletImg = cardTablet ? cardTablet.querySelector('img, canvas') : null;
            return {
                modalOpen: modal ? modal.classList.contains('qr_modal_open') : false,
                tabletTitle: titleTablet ? titleTablet.innerText : '',
                serverCardVisible: cardServer ? window.getComputedStyle(cardServer).display !== 'none' : false,
                tabletUrl: urlTablet ? urlTablet.innerText : '',
                hasQrImage: !!qrTabletImg
            };
        })()`);

        check('QR standalone: модальное окно открыто', qrStandalone.modalOpen);
        check('QR standalone: заголовок «Планшет (автономный режим)»', qrStandalone.tabletTitle.includes('автономный'));
        check('QR standalone: карточка сервера скрыта (только 1 QR-код)', !qrStandalone.serverCardVisible);
        check('QR standalone: URL планшета корректный', qrStandalone.tabletUrl === 'http://192.168.1.50:8080/setup.html');
        check('QR standalone: сгенерирован QR-код (img или canvas)', qrStandalone.hasQrImage);

        // 5b: Dual QR mode (server discovered)
        const qrServerMode = await cdp.evaluate(`(async () => {
            const origAjax = $.ajax;
            $.ajax = function(opts) {
                const d = $.Deferred();
                setTimeout(() => {
                    d.resolve({ status: "ok", device_ip: "192.168.1.50", server_url: "http://192.168.1.100:8085" });
                }, 10);
                return d.promise();
            };
            show_qr_onboarding();
            await new Promise(r => setTimeout(r, 200));
            $.ajax = origAjax;
            const cardServer = document.getElementById('card_qr_server');
            const titleTablet = document.getElementById('title_qr_tablet');
            const urlServer = document.getElementById('url_qr_server');
            const qrServerImg = cardServer ? cardServer.querySelector('img, canvas') : null;
            return {
                tabletTitle: titleTablet ? titleTablet.innerText : '',
                serverCardVisible: cardServer ? window.getComputedStyle(cardServer).display !== 'none' : false,
                serverUrl: urlServer ? urlServer.innerText : '',
                hasServerQrImage: !!qrServerImg
            };
        })()`);

        check('QR server mode: заголовок «Планшет»', qrServerMode.tabletTitle === 'Планшет');
        check('QR server mode: карточка сервера отображается (2 QR-кода)', qrServerMode.serverCardVisible);
        check('QR server mode: URL сервера корректный', qrServerMode.serverUrl === 'http://192.168.1.100:8085/setup.html');
        check('QR server mode: сгенерирован QR-код сервера', qrServerMode.hasServerQrImage);

        // 5c: Close QR modal
        const qrClosed = await cdp.evaluate(`(() => {
            const btn = document.getElementById('qr_close_btn');
            if (btn) btn.click();
            const modal = document.getElementById('qr_modal');
            return modal ? !modal.classList.contains('qr_modal_open') : false;
        })()`);
        check('QR modal: кнопка Закрыть скрывает окно', qrClosed);

        // 5d: Menu top back button
        const menuBackRes = await cdp.evaluate(`(() => {
            const menu = document.getElementById('weather_menu');
            const btn = document.getElementById('menu_btn');
            if (btn) btn.click();
            const opened = menu ? menu.classList.contains('menu_open') : false;
            const backBtn = document.getElementById('menu_top_back');
            if (backBtn) backBtn.click();
            const closed = menu ? !menu.classList.contains('menu_open') : false;
            return { opened: opened, closed: closed };
        })()`);
        check('Меню: кнопка ← Назад в шапке закрывает меню', menuBackRes.opened && menuBackRes.closed);

        // ==========================================
        // 5e. Tomorrow Forecast Modal (#modal_tomorrow_forecast)
        // ==========================================
        console.log('\n[5e] Tomorrow Forecast Modal:');
        const mtfRes = await cdp.evaluate(`(() => {
            const mockData = {
                src: "Yandex",
                now: 1794770000,
                fact: {
                    temp: 2,
                    feels_like: -1,
                    condition: "cloudy",
                    icon: "bkn_d",
                    wind_speed: 3.5,
                    wind_angle: 220,
                    humidity: 78,
                    pressure_mm: 750
                },
                forecasts: [
                    { date: "2026-11-15", sunrise: "07:41", sunset: "15:55", parts: {} },
                    {
                        date: "2026-11-16",
                        sunrise: "07:43",
                        sunset: "15:53",
                        parts: {
                            night: { temp_min: 0, temp_max: 1, temp_avg: 0, feels_like: -4, icon: "bkn_n", condition: "cloudy", humidity: 82, wind_speed: 3.2, wind_angle: 230, pressure_mm: 751 },
                            morning: { temp_min: 0, temp_max: 2, temp_avg: 1, feels_like: -3, icon: "ovc", condition: "overcast", humidity: 80, wind_speed: 3.8, wind_angle: 220, pressure_mm: 753 },
                            day: { temp_min: 2, temp_max: 4, temp_avg: 3, feels_like: 0, icon: "ovc_ra", condition: "rain", humidity: 85, wind_speed: 4.5, wind_gust: 7.2, wind_angle: 210, prec_mm: 1.2, pressure_mm: 748 },
                            evening: { temp_min: 1, temp_max: 2, temp_avg: 1, feels_like: -2, icon: "ovc", condition: "overcast", humidity: 83, wind_speed: 3.0, wind_angle: 200, pressure_mm: 749 }
                        }
                    }
                ]
            };
            window.current_weather_data = mockData;

            // 1. Клик по блоку прогноза
            const forecastBlock = document.getElementById('forecast');
            if (forecastBlock) forecastBlock.click();

            const modal = document.getElementById('modal_tomorrow_forecast');
            const isOpen = modal ? modal.classList.contains('mtf_open') : false;

            const tNight = document.getElementById('mtf_temp_night') ? document.getElementById('mtf_temp_night').innerText : '';
            const tDay = document.getElementById('mtf_temp_day') ? document.getElementById('mtf_temp_day').innerText : '';
            const precDay = document.getElementById('mtf_prec_day') ? document.getElementById('mtf_prec_day').innerText : '';
            const windDay = document.getElementById('mtf_wind_day') ? document.getElementById('mtf_wind_day').innerText : '';
            const pressDay = document.getElementById('mtf_press_day') ? document.getElementById('mtf_press_day').innerText : '';
            const pressNight = document.getElementById('mtf_press_night') ? document.getElementById('mtf_press_night').innerText : '';
            const sunrise = document.getElementById('mtf_sunrise') ? document.getElementById('mtf_sunrise').innerText : '';
            const sunset = document.getElementById('mtf_sunset') ? document.getElementById('mtf_sunset').innerText : '';
            const duration = document.getElementById('mtf_day_duration') ? document.getElementById('mtf_day_duration').innerText : '';

            // Тест переключения табов 24ч -> 48ч
            const tab48 = document.getElementById('mtf_tab_48');
            if (tab48) tab48.click();
            const isTab48Active = tab48 ? tab48.classList.contains('active') : false;
            const subtitle48 = document.getElementById('mtf_subtitle') ? document.getElementById('mtf_subtitle').innerText : '';
            const tab24 = document.getElementById('mtf_tab_24');
            if (tab24) tab24.click();
            const isTab24Active = tab24 ? tab24.classList.contains('active') : false;
            const durationIcon = document.getElementById('duration_icon') ? document.getElementById('duration_icon').innerText : '';

            // 2. Закрытие по клику на крестик
            const closeBtn = document.getElementById('mtf_close_btn');
            if (closeBtn) closeBtn.click();
            const isClosedByBtn = modal ? !modal.classList.contains('mtf_open') : false;

            // 3. Повторное открытие и закрытие по Escape
            if (forecastBlock) forecastBlock.click();
            const isReopened = modal ? modal.classList.contains('mtf_open') : false;
            const escEvent = new KeyboardEvent('keydown', { keyCode: 27 });
            document.dispatchEvent(escEvent);
            const isClosedByEsc = modal ? !modal.classList.contains('mtf_open') : false;

            return {
                isOpen,
                tNight,
                tDay,
                precDay,
                windDay,
                pressDay,
                pressNight,
                sunrise,
                sunset,
                duration,
                isTab48Active,
                subtitle48,
                isTab24Active,
                durationIcon,
                isClosedByBtn,
                isReopened,
                isClosedByEsc
            };
        })()`);

        check('MTF: клик по #forecast открывает модальное окно', mtfRes.isOpen);
        check('MTF: температура ночи корректна (0°..+1°)', mtfRes.tNight === '0°..+1°');
        check('MTF: температура дня корректна (+2°..+4°)', mtfRes.tDay === '+2°..+4°');
        check('MTF: осадки дня отображаются (1.2 мм)', mtfRes.precDay === '1.2 мм');
        check('MTF: ветер дня отображается с порывами (4.5 м/с (до 7.2))', mtfRes.windDay.includes('4.5 м/с') && mtfRes.windDay.includes('7.2'));
        check('MTF: давление ночи показывает рост (751 мм ↗ +1)', mtfRes.pressNight.includes('751') && mtfRes.pressNight.includes('+1'));
        check('MTF: давление дня показывает падение (748 мм ↘ -2)', mtfRes.pressDay.includes('748') && mtfRes.pressDay.includes('-2'));
        check('MTF: восход и закат на завтра отображаются (07:43 / 15:53)', mtfRes.sunrise === '07:43' && mtfRes.sunset === '15:53');
        check('MTF: долгота дня рассчитана (8 ч 10 мин)', mtfRes.duration === '8 ч 10 мин');
        check('MTF: таб 48ч активируется по клику', mtfRes.isTab48Active && mtfRes.subtitle48.includes('48ч'));
        check('MTF: таб 24ч возвращается в активное состояние', mtfRes.isTab24Active);
        check('UI: иконка дня duration_icon присутствует', mtfRes.durationIcon === '☀️');
        check('MTF: кнопка ✕ закрывает модальное окно', mtfRes.isClosedByBtn);
        check('MTF: клавиша Esc закрывает модальное окно', mtfRes.isReopened && mtfRes.isClosedByEsc);

        // ==========================================
        // 5f. Settings Page UI, Dual QR & Idle Timer
        // ==========================================
        console.log('\n[5f] Settings Page UI, Dual QR & Idle Timer:');
        await cdp.send('Page.navigate', { url: `file://${SETTINGS_PATH}` });
        await delay(500);

        const settingsRes = await cdp.evaluate(`(() => {
            const idleCd = document.getElementById('idle_countdown');
            const idleCdBottom = document.getElementById('idle_countdown_bottom');
            const idleBadge = document.getElementById('idle_badge');
            const topBack = document.getElementById('top_back_btn');
            const bottomBack = document.getElementById('bottom_back_btn');

            // Тестируем мок конфигурации с реальным IP и сервером
            currentCfg = {
                device_ip: "192.168.4.5",
                server_url: "http://192.168.1.55:8085/weather.json"
            };
            renderSettingsQR();

            const qrTablet = document.getElementById('settings_qr_box');
            const qrServer = document.getElementById('settings_qr_box_server');
            const qrColServer = document.getElementById('qr_col_server');
            const qrUrl = document.getElementById('settings_qr_url');
            const qrUrlServer = document.getElementById('settings_qr_url_server');

            // Проверка сброса таймера
            idleSecondsLeft = 20;
            updateIdleBadge();
            const wasReduced = idleSecondsLeft === 20;
            window.dispatchEvent(new Event('mousemove'));
            const isReset = idleSecondsLeft === 60;

            return {
                hasIdleCountdown: !!idleCd && idleCd.innerText === '60',
                hasIdleBottom: !!idleCdBottom && idleCdBottom.innerText === '60',
                hasIdleBadge: !!idleBadge,
                hasTopBack: !!topBack,
                hasBottomBack: !!bottomBack,
                tabletUrl: qrUrl ? qrUrl.innerText : '',
                serverUrl: qrUrlServer ? qrUrlServer.innerText : '',
                serverColVisible: qrColServer ? qrColServer.style.display !== 'none' : false,
                hasQrTabletImage: !!(qrTablet && qrTablet.querySelector('img, canvas')),
                hasQrServerImage: !!(qrServer && qrServer.querySelector('img, canvas')),
                timerResetOnActivity: wasReduced && isReset
            };
        })()`);

        check('Settings: таймер обратного отсчета (60с) присутствует в шапке', settingsRes.hasIdleCountdown);
        check('Settings: таймер обратного отсчета присутствует на кнопке возврата', settingsRes.hasIdleBottom);
        check('Settings: бейдж автовозврата присутствует', settingsRes.hasIdleBadge);
        check('Settings: кнопка возврата в шапке присутствует', settingsRes.hasTopBack);
        check('Settings: кнопка возврата в подвале присутствует', settingsRes.hasBottomBack);
        check('Settings: URL планшета использует реальный IP (не 127.0.0.1)', settingsRes.tabletUrl.includes('192.168.4.5'));
        check('Settings: QR-код сервера отображается при настроенном server_url', settingsRes.serverColVisible);
        check('Settings: URL сервера корректный (/setup.html)', settingsRes.serverUrl === 'http://192.168.1.55:8085/setup.html');
        check('Settings: сгенерирован QR-код планшета', settingsRes.hasQrTabletImage);
        check('Settings: сгенерирован QR-код сервера', settingsRes.hasQrServerImage);
        check('Settings: активность пользователя сбрасывает таймер на 60с', settingsRes.timerResetOnActivity);

        // ==========================================
        // 5g. Setup Page UI, Form Inputs & Contract
        // ==========================================
        console.log('\n[5g] Mobile Setup Page UI & Key Contract:');
        await cdp.send('Page.navigate', { url: `file://${SETUP_PATH}` });
        await delay(500);

        const setupRes = await cdp.evaluate(`(() => {
            const idleCd = document.getElementById('idle_countdown');
            const navTitle = document.getElementById('setup_nav_title');
            const targetBadge = document.getElementById('nav_target_badge');
            const backBtn = document.getElementById('btn_back_informer');
            const srcSel = document.getElementById('primary_source');
            const optOm = srcSel ? srcSel.querySelector('option[value="om"]') : null;
            const optOpenMeteo = srcSel ? srcSel.querySelector('option[value="openmeteo"]') : null;

            const apiInp = document.getElementById('api');
            const gisInp = document.getElementById('gismeteo_api_key');
            const forInp = document.getElementById('foreca_api_key');
            const owmInp = document.getElementById('openweathermap_api_key');

            const hasAllKeyInputs = !!(apiInp && gisInp && forInp && owmInp);
            const allDisableAutoCorrect = [apiInp, gisInp, forInp, owmInp].every(el =>
                el && el.getAttribute('autocorrect') === 'off' && el.getAttribute('autocapitalize') === 'off'
            );

            return {
                hasIdleCountdown: !!idleCd && idleCd.innerText === '60',
                hasNavTitle: !!navTitle && navTitle.innerText.length > 0,
                hasTargetBadge: !!targetBadge,
                hasBackBtn: !!backBtn,
                hasOptOm: !!optOm,
                noLegacyOptOpenMeteo: !optOpenMeteo,
                hasAllKeyInputs,
                allDisableAutoCorrect
            };
        })()`);

        check('Setup: таймер обратного отсчета (60с) присутствует', setupRes.hasIdleCountdown);
        check('Setup: заголовок навигации присутствует', setupRes.hasNavTitle);
        check('Setup: бейдж роли присутствует', setupRes.hasTargetBadge);
        check('Setup: кнопка возврата присутствует', setupRes.hasBackBtn);
        check('Setup: провайдер Open-Meteo использует стандартный код "om"', setupRes.hasOptOm && setupRes.noLegacyOptOpenMeteo);
        check('Setup: присутствуют поля всех 4 API-ключей провайдеров', setupRes.hasAllKeyInputs);
        check('Setup: все поля ключей отключают автокоррекцию/автокапитализацию', setupRes.allDisableAutoCorrect);

        // Возврат на informer.html перед скриншотами
        await cdp.send('Page.navigate', { url: `file://${HTML_PATH}` });
        await delay(500);

        // ==========================================
        // 6. Visual Screenshots
        // ==========================================
        console.log('\n[6] Capturing Visual Verification Screenshots:');
        const shot1024 = await cdp.send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync('/tmp/informer_test_1024x600.png', Buffer.from(shot1024.result.data, 'base64'));
        console.log('  Saved /tmp/informer_test_1024x600.png');

        await cdp.send('Emulation.setDeviceMetricsOverride', {
            width: 1280,
            height: 800,
            deviceScaleFactor: 1,
            mobile: false
        });
        await delay(400);
        const shot1280 = await cdp.send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync('/tmp/informer_test_1280x800.png', Buffer.from(shot1280.result.data, 'base64'));
        console.log('  Saved /tmp/informer_test_1280x800.png');

    } catch (err) {
        fail++;
        console.error('Test execution error:', err);
    } finally {
        if (cdp) cdp.close();
        chrome.kill('SIGKILL');
    }

    console.log(`\nИТОГ UI TEST: pass=${pass} fail=${fail}`);
    process.exit(fail > 0 ? 1 : 0);
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
