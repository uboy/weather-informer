package ru.weather.informer;

import android.content.Context;
import android.util.Log;
import org.json.JSONObject;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
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
    private volatile java.lang.ref.WeakReference<MainActivity> activityRef;
    private volatile String discoveredServerUrl = "";
    private volatile String deviceIp = "";

    public String getDiscoveredServerUrl() {
        return discoveredServerUrl;
    }

    public void setDiscoveredServerUrl(String url) {
        this.discoveredServerUrl = (url != null) ? url : "";
    }

    public String getDeviceIp() {
        if (deviceIp != null && !deviceIp.isEmpty() && !"127.0.0.1".equals(deviceIp)) {
            return deviceIp;
        }
        deviceIp = resolveLocalIpAddress();
        return deviceIp;
    }

    public static String resolveLocalIpAddress() {
        try {
            Enumeration<NetworkInterface> en = NetworkInterface.getNetworkInterfaces();
            while (en != null && en.hasMoreElements()) {
                NetworkInterface intf = en.nextElement();
                if (!intf.isUp() || intf.isLoopback()) continue;
                Enumeration<InetAddress> enumIpAddr = intf.getInetAddresses();
                while (enumIpAddr.hasMoreElements()) {
                    InetAddress inetAddress = enumIpAddr.nextElement();
                    if (!inetAddress.isLoopbackAddress() && inetAddress instanceof Inet4Address) {
                        return inetAddress.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {}
        return "127.0.0.1";
    }

    public void startUdpDiscoveryAsync() {
        Thread t = new Thread(new UdpDiscoveryWorker(this), "UdpDiscoveryThread");
        t.setDaemon(true);
        t.start();
    }

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
        this.activityRef = (activity != null) ? new java.lang.ref.WeakReference<>(activity) : null;
    }

    public MainActivity getActivity() {
        java.lang.ref.WeakReference<MainActivity> ref = this.activityRef;
        return (ref != null) ? ref.get() : null;
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
        startUdpDiscoveryAsync();
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
        File f = new File(webDir, "settings.html");
        if (f.exists() && f.isFile()) {
            return new String(readFileBytes(f), StandardCharsets.UTF_8);
        }
        try (InputStream is = context.getAssets().open("settings.html")) {
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) != -1) baos.write(buf, 0, n);
            return baos.toString("UTF-8");
        } catch (Exception ignored) {}
        return "<!DOCTYPE html><html><head><meta http-equiv='refresh' content='0; url=/settings.html'></head><body><script>window.location.href='/settings.html';</script></body></html>";
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
