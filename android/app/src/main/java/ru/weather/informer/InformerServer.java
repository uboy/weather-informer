package ru.weather.informer;

import android.content.Context;
import android.util.Log;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;

public class InformerServer {
    private static final String TAG = "InformerServer";
    public static final int PORT = 8080;
    public static final int MAX_UPLOAD_BYTES = 5 * 1024 * 1024; // 5 MB

    private final Context context;
    private final File webDir;
    private ServerSocket serverSocket;
    private ExecutorService threadPool;
    private volatile boolean isRunning = false;
    private final long startTime = System.currentTimeMillis();
    private static InformerServer instance;
    private MainActivity activity;

    public InformerServer(Context context) {
        this.context = context.getApplicationContext();
        this.webDir = new File(context.getFilesDir(), "web");
        if (!webDir.exists()) {
            webDir.mkdirs();
        }
        instance = this;
    }

    public static InformerServer getInstance() {
        return instance;
    }

    public void setActivity(MainActivity activity) {
        this.activity = activity;
    }

    public MainActivity getActivity() {
        return activity;
    }

    public Context getContext() {
        return context;
    }

    public File getWebDir() {
        return webDir;
    }

    public long getStartTime() {
        return startTime;
    }

    public boolean isRunning() {
        return isRunning;
    }

    public synchronized void start() throws IOException {
        if (isRunning) return;
        serverSocket = new ServerSocket();
        serverSocket.setReuseAddress(true);
        serverSocket.bind(new InetSocketAddress(PORT));
        threadPool = Executors.newCachedThreadPool();
        isRunning = true;

        threadPool.execute(new ServerAcceptor(this, serverSocket, threadPool));
    }

    public synchronized void stop() {
        isRunning = false;
        try {
            if (serverSocket != null && !serverSocket.isClosed()) {
                serverSocket.close();
            }
        } catch (IOException ignored) {}
        if (threadPool != null) {
            threadPool.shutdownNow();
        }
        Log.i(TAG, "Informer HTTP Server stopped");
    }

    public byte[] readConfigFile() {
        File f = new File(webDir, "config.json");
        if (f.exists() && f.isFile()) {
            return readFileBytes(f);
        }
        try (InputStream is = context.getAssets().open("config.json")) {
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = is.read(buf)) != -1) baos.write(buf, 0, n);
            return baos.toByteArray();
        } catch (Exception e) {
            return "{}".getBytes(StandardCharsets.UTF_8);
        }
    }

    private static byte[] readFileBytes(File file) {
        try (FileInputStream fis = new FileInputStream(file)) {
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = fis.read(buf)) != -1) baos.write(buf, 0, n);
            return baos.toByteArray();
        } catch (Exception e) {
            return new byte[0];
        }
    }

    public String getSettingsHtml() {
        return "<!DOCTYPE html><html><head><meta charset='utf-8'>" +
                "<meta name='viewport' content='width=device-width, initial-scale=1.0'>" +
                "<title>Настройки Погодного Информера</title>" +
                "<style>" +
                "body { font-family: -apple-system, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 16px; }" +
                ".card { background: #1e1e1e; border-radius: 12px; padding: 20px; max-width: 480px; margin: 0 auto; box-shadow: 0 4px 16px rgba(0,0,0,0.5); }" +
                "h2 { margin-top: 0; color: #4FC3F7; font-size: 1.4rem; }" +
                "label { display: block; margin-top: 14px; font-size: 0.9rem; color: #bbb; }" +
                "input, select { width: 100%; box-sizing: border-box; padding: 10px; border-radius: 8px; border: 1px solid #333; background: #2a2a2a; color: #fff; margin-top: 5px; font-size: 1rem; }" +
                "button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer; margin-top: 18px; }" +
                ".btn-primary { background: #0288D1; color: #fff; }" +
                ".btn-sec { background: #37474F; color: #fff; margin-top: 10px; }" +
                ".btn-gps { background: #2E7D32; color: #fff; margin-top: 6px; padding: 8px; font-size: 0.85rem; }" +
                "#status { margin-top: 14px; padding: 10px; border-radius: 6px; display: none; text-align: center; }" +
                ".links { margin-top: 20px; text-align: center; font-size: 0.9rem; }" +
                ".links a { color: #4FC3F7; text-decoration: none; margin: 0 8px; }" +
                "</style></head><body>" +
                "<div class='card'>" +
                "<h2>Погодный Информер: Настройки</h2>" +
                "<label>Широта (Latitude):</label><input type='number' step='0.0001' id='lat'>" +
                "<label>Долгота (Longitude):</label><input type='number' step='0.0001' id='lon'>" +
                "<button type='button' class='btn-gps' id='btn_gps'>Определить GPS со смартфона</button>" +
                "<label>Город (для отображения):</label>" +
                "<div style='display:flex; gap:6px; align-items:center;'>" +
                "<input type='text' id='city_name' placeholder='Определяется автоматически'>" +
                "<button type='button' class='btn-gps' id='btn_detect_city' style='width:auto; margin-top:5px; white-space:nowrap; background:#00838F; padding:10px 14px;'>Авто</button>" +
                "</div>" +
                "<label>Адрес домашнего сервера (server_url):</label><input type='text' id='server_url' placeholder='http://192.168.1.55:8085'>" +
                "<label>Основной источник погоды:</label>" +
                "<select id='primary_source'>" +
                "<option value='server'>Локальный сервер (рекомендуется)</option>" +
                "<option value='yandex'>Яндекс.Погода (API)</option>" +
                "<option value='gismeteo'>Gismeteo v2 (API Token)</option>" +
                "<option value='foreca'>Foreca (API Token)</option>" +
                "<option value='om'>Open-Meteo (бесплатно, без ключа)</option>" +
                "<option value='owm'>OpenWeatherMap (API Key)</option>" +
                "<option value='7timer'>7timer (бесплатно, без ключа)</option>" +
                "<option value='wttr'>wttr.in (бесплатно, без ключа)</option>" +
                "</select>" +
                "<label>Интервал синхронизации с сервером, сек (по умолчанию 300 = 5 мин):</label>" +
                "<input type='number' id='update_interval_sec' min='60' step='30'>" +
                "<label>Интервал прямых запросов к внешним API, сек (по умолчанию 1800 = 30 мин):</label>" +
                "<input type='number' id='direct_update_interval_sec' min='60' step='30'>" +
                "<label>Ключ Gismeteo API v2:</label>" +
                "<div style='display:flex; gap:6px; align-items:center;'>" +
                "<input type='text' id='gismeteo_api_key' placeholder='X-Gismeteo-Token'>" +
                "<button type='button' class='btn-gps' id='btn_clear_gismeteo' style='width:auto; margin-top:5px; white-space:nowrap; background:#C62828; padding:10px 14px;'>✕</button>" +
                "</div>" +
                "<label>Ключ Яндекс.Погода:</label>" +
                "<div style='display:flex; gap:6px; align-items:center;'>" +
                "<input type='text' id='api' placeholder='X-Yandex-Weather-Key'>" +
                "<button type='button' class='btn-gps' id='btn_clear_yandex' style='width:auto; margin-top:5px; white-space:nowrap; background:#C62828; padding:10px 14px;'>✕</button>" +
                "</div>" +
                "<label>Ключ Foreca (API Token):</label>" +
                "<div style='display:flex; gap:6px; align-items:center;'>" +
                "<input type='text' id='foreca_api_key' placeholder='Bearer JWT Token'>" +
                "<button type='button' class='btn-gps' id='btn_clear_foreca' style='width:auto; margin-top:5px; white-space:nowrap; background:#C62828; padding:10px 14px;'>✕</button>" +
                "</div>" +
                "<label>Ключ OpenWeatherMap (API Key):</label>" +
                "<div style='display:flex; gap:6px; align-items:center;'>" +
                "<input type='text' id='openweathermap_api_key' placeholder='API Key'>" +
                "<button type='button' class='btn-gps' id='btn_clear_owm' style='width:auto; margin-top:5px; white-space:nowrap; background:#C62828; padding:10px 14px;'>✕</button>" +
                "</div>" +
                "<button type='button' class='btn-primary' id='btn_save'>Сохранить настройки</button>" +
                "<button type='button' class='btn-sec' id='btn_reload'>Обновить экран информера</button>" +
                "<div id='status'></div>" +
                "<div class='links'>" +
                "<a href='/'>Посмотреть информер</a> | <a href='/upload'>Загрузка файлов</a>" +
                "</div></div>" +
                "<script>" +
                "var currentCfg = {};" +
                "fetch('/api/settings').then(function(r){return r.json();}).then(function(c){" +
                "currentCfg = c;" +
                "document.getElementById('lat').value = c.lat || '';" +
                "document.getElementById('lon').value = c.lon || '';" +
                "document.getElementById('city_name').value = c.city_name || '';" +
                "document.getElementById('server_url').value = c.server_url || '';" +
                "document.getElementById('primary_source').value = c.primary_source || (c.server_url ? 'server' : 'yandex');" +
                "document.getElementById('update_interval_sec').value = c.update_interval_sec || c.timeout || 300;" +
                "document.getElementById('direct_update_interval_sec').value = c.direct_update_interval_sec || 1800;" +
                "document.getElementById('gismeteo_api_key').value = c.gismeteo_api_key || '';" +
                "document.getElementById('api').value = c.api || '';" +
                "document.getElementById('foreca_api_key').value = c.foreca_api_key || '';" +
                "document.getElementById('openweathermap_api_key').value = c.openweathermap_api_key || '';" +
                "});" +
                "document.getElementById('btn_clear_gismeteo').onclick = function(){" +
                "document.getElementById('gismeteo_api_key').value = '';" +
                "currentCfg.gismeteo_api_key = '';" +
                "};" +
                "document.getElementById('btn_clear_yandex').onclick = function(){" +
                "document.getElementById('api').value = '';" +
                "currentCfg.api = '';" +
                "};" +
                "document.getElementById('btn_clear_foreca').onclick = function(){" +
                "document.getElementById('foreca_api_key').value = '';" +
                "currentCfg.foreca_api_key = '';" +
                "};" +
                "document.getElementById('btn_clear_owm').onclick = function(){" +
                "document.getElementById('openweathermap_api_key').value = '';" +
                "currentCfg.openweathermap_api_key = '';" +
                "};" +
                "function detectCity(la, lo){" +
                "if(!la || !lo) return Promise.resolve(null);" +
                "var cInput = document.getElementById('city_name');" +
                "var tok = document.getElementById('gismeteo_api_key').value.trim();" +
                "var q = '/api/reverse?lat=' + la + '&lon=' + lo + (tok ? ('&token=' + encodeURIComponent(tok)) : '');" +
                "cInput.placeholder = 'Определение города...';" +
                "return fetch(q).then(function(r){return r.json();}).then(function(res){" +
                "if(res && res.name){" +
                "cInput.value = res.name;" +
                "currentCfg.city_name = res.name;" +
                "var s = document.getElementById('status');" +
                "s.style.display = 'block'; s.style.background = '#00695C';" +
                "s.innerText = 'Город определён: ' + res.name + (res.source ? ' [' + res.source + ']' : '');" +
                "setTimeout(function(){ s.style.display='none'; }, 3500);" +
                "return res.name;" +
                "} else {" +
                "cInput.placeholder = 'Введите город вручную';" +
                "return null;" +
                "}" +
                "}).catch(function(){" +
                "cInput.placeholder = 'Введите город вручную';" +
                "return null;" +
                "});" +
                "}" +
                "document.getElementById('btn_gps').onclick = function(){" +
                "if(!navigator.geolocation){alert('Геолокация недоступна'); return;}" +
                "navigator.geolocation.getCurrentPosition(function(p){" +
                "var la = p.coords.latitude.toFixed(4);" +
                "var lo = p.coords.longitude.toFixed(4);" +
                "document.getElementById('lat').value = la;" +
                "document.getElementById('lon').value = lo;" +
                "detectCity(la, lo);" +
                "}, function(e){alert('Ошибка GPS: ' + e.message);});" +
                "};" +
                "document.getElementById('btn_detect_city').onclick = function(){" +
                "var la = document.getElementById('lat').value.trim();" +
                "var lo = document.getElementById('lon').value.trim();" +
                "if(!la || !lo){alert('Сначала укажите координаты'); return;}" +
                "detectCity(la, lo);" +
                "};" +
                "function doSave(){" +
                "currentCfg.lat = parseFloat(document.getElementById('lat').value);" +
                "currentCfg.lon = parseFloat(document.getElementById('lon').value);" +
                "currentCfg.city_name = document.getElementById('city_name').value.trim();" +
                "currentCfg.server_url = document.getElementById('server_url').value.trim();" +
                "currentCfg.primary_source = document.getElementById('primary_source').value;" +
                "currentCfg.update_interval_sec = Math.max(60, parseInt(document.getElementById('update_interval_sec').value, 10) || 300);" +
                "currentCfg.direct_update_interval_sec = Math.max(60, parseInt(document.getElementById('direct_update_interval_sec').value, 10) || 1800);" +
                "currentCfg.timeout = currentCfg.update_interval_sec;" +
                "currentCfg.gismeteo_api_key = document.getElementById('gismeteo_api_key').value.trim();" +
                "currentCfg.api = document.getElementById('api').value.trim();" +
                "currentCfg.foreca_api_key = document.getElementById('foreca_api_key').value.trim();" +
                "currentCfg.openweathermap_api_key = document.getElementById('openweathermap_api_key').value.trim();" +
                "var s = document.getElementById('status');" +
                "fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(currentCfg)})" +
                ".then(function(r){return r.json();}).then(function(res){" +
                "s.style.display='block'; s.style.background='#1B5E20'; s.innerText='Настройки сохранены! Экран обновлён.';" +
                "setTimeout(function(){s.style.display='none';}, 4000);" +
                "}).catch(function(e){" +
                "s.style.display='block'; s.style.background='#B71C1C'; s.innerText='Ошибка: ' + e;" +
                "});" +
                "}" +
                "document.getElementById('btn_save').onclick = function(){" +
                "var la = document.getElementById('lat').value.trim();" +
                "var lo = document.getElementById('lon').value.trim();" +
                "var cn = document.getElementById('city_name').value.trim();" +
                "if(!cn && la && lo){" +
                "detectCity(la, lo).then(function(){ doSave(); });" +
                "} else {" +
                "doSave();" +
                "}" +
                "};" +
                "document.getElementById('btn_reload').onclick = function(){" +
                "fetch('/api/reload', {method:'POST'}).then(function(){alert('Экран обновлён!');});" +
                "};" +
                "</script></body></html>";
    }

    public String getUploadHtml() {
        return "<!DOCTYPE html><html><head><meta charset='utf-8'>" +
                "<meta name='viewport' content='width=device-width, initial-scale=1.0'>" +
                "<title>Загрузка файлов на планшет</title>" +
                "<style>" +
                "body { font-family: -apple-system, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 16px; }" +
                ".card { background: #1e1e1e; border-radius: 12px; padding: 20px; max-width: 480px; margin: 0 auto; box-shadow: 0 4px 16px rgba(0,0,0,0.5); }" +
                "h2 { margin-top: 0; color: #4FC3F7; font-size: 1.4rem; }" +
                "p { color: #aaa; font-size: 0.9rem; line-height: 1.4; }" +
                ".dropzone { border: 2px dashed #0288D1; border-radius: 8px; padding: 30px; text-align: center; margin: 15px 0; background: #182228; cursor: pointer; }" +
                "input[type=file] { display: none; }" +
                "button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer; background: #0288D1; color: #fff; margin-top: 10px; }" +
                "#status { margin-top: 14px; padding: 10px; border-radius: 6px; display: none; text-align: center; font-size: 0.9rem; }" +
                ".links { margin-top: 20px; text-align: center; font-size: 0.9rem; }" +
                ".links a { color: #4FC3F7; text-decoration: none; margin: 0 8px; }" +
                "</style></head><body>" +
                "<div class='card'>" +
                "<h2>Загрузка файлов по воздуху</h2>" +
                "<p>Загрузите новую версию <code>informer.html</code>, <code>jquery.min.js</code>, <code>config.json</code> или картинки без подключения кабеля и без ADB.</p>" +
                "<div class='dropzone' id='dropzone'>Нажмите здесь для выбора файла<br>(или перетащите сюда)</div>" +
                "<input type='file' id='file_input'>" +
                "<div id='file_name' style='text-align:center; color:#4FC3F7; font-size:0.95rem; margin-bottom:10px;'></div>" +
                "<button type='button' id='btn_upload' disabled>Загрузить на планшет</button>" +
                "<div id='status'></div>" +
                "<div class='links'>" +
                "<a href='/settings'>Настройки</a> | <a href='/'>Открыть информер</a>" +
                "</div></div>" +
                "<script>" +
                "var dz = document.getElementById('dropzone');" +
                "var fi = document.getElementById('file_input');" +
                "var btn = document.getElementById('btn_upload');" +
                "var fn = document.getElementById('file_name');" +
                "var st = document.getElementById('status');" +
                "var selectedFile = null;" +
                "dz.onclick = function(){ fi.click(); };" +
                "fi.onchange = function(){" +
                "if(fi.files.length > 0){" +
                "selectedFile = fi.files[0];" +
                "fn.innerText = selectedFile.name + ' (' + Math.round(selectedFile.size/1024) + ' KB)';" +
                "btn.disabled = false;" +
                "}" +
                "};" +
                "btn.onclick = function(){" +
                "if(!selectedFile) return;" +
                "btn.disabled = true; btn.innerText = 'Загрузка...';" +
                "var fd = new FormData();" +
                "fd.append('file', selectedFile);" +
                "fetch('/api/upload', {method:'POST', body:fd})" +
                ".then(function(r){return r.json();}).then(function(res){" +
                "btn.innerText = 'Загрузить на планшет';" +
                "if(res.error){" +
                "st.style.display='block'; st.style.background='#B71C1C'; st.innerText = 'Ошибка: ' + res.error;" +
                "} else {" +
                "st.style.display='block'; st.style.background='#1B5E20'; st.innerText = 'Успешно загружен ' + res.file + '! Экран обновлен.';" +
                "btn.disabled = false;" +
                "}" +
                "}).catch(function(e){" +
                "btn.innerText = 'Загрузить на планшет'; btn.disabled = false;" +
                "st.style.display='block'; st.style.background='#B71C1C'; st.innerText = 'Ошибка сети: ' + e;" +
                "});" +
                "};" +
                "</script></body></html>";
    }
}
