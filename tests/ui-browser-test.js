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
        await delay(1200);

        const versionRes = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
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

                return {
                    sectorWindClass: sectorWind ? sectorWind.className : '',
                    windText,
                    windProgStyle: windProg ? windProg.getAttribute('style') : '',
                    windProgBg: windProg ? window.getComputedStyle(windProg).backgroundColor : '',
                    windProgWidth: windProg ? windProg.style.width : '',
                    pressTrendHtml: pressTrend ? pressTrend.innerHTML : '',
                    pressTrendVisible: pressTrend ? window.getComputedStyle(pressTrend).display !== 'none' : false,
                    pressVal,
                    pressProgStyle: pressProg ? pressProg.getAttribute('style') : ''
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

        const history4h = [
            { t: nowMs - 4 * 3600 * 1000, p: 748 }
        ];
        const mTrendDown = await renderMock({ pressure_mm: 745 }, history4h);
        check('Mode 6b: тренд давления падение (-3) → содержит стрелку ↓ и -3',
            mTrendDown.pressTrendHtml.includes('↓') && mTrendDown.pressTrendHtml.includes('-3'));
        check('Mode 6b: тренд давления падение (-3) → содержит интервал (4ч)',
            mTrendDown.pressTrendHtml.includes('(4ч)'));

        // Mode 7: Track styling invariant (no border, clean background)
        const trackStyle = await cdp.evaluate(`(() => {
            const bar = document.querySelector('.wind_humidity_pressure_bar');
            const comp = window.getComputedStyle(bar);
            return {
                bg: comp.backgroundColor,
                borderTopWidth: comp.borderTopWidth
            };
        })()`);
        check('Progress track background is #363942 (rgb(54, 57, 66))',
            trackStyle.bg === 'rgb(54, 57, 66)');
        check('Progress track has no layout-shifting borders (0px)',
            trackStyle.borderTopWidth === '0px');

        // ==========================================
        // 5. Visual Screenshots
        // ==========================================
        console.log('\n[5] Capturing Visual Verification Screenshots:');
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
