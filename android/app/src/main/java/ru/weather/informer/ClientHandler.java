package ru.weather.informer;

import android.util.Log;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class ClientHandler implements Runnable {
    private static final String TAG = "ClientHandler";
    private final InformerServer server;
    private final Socket socket;

    public ClientHandler(InformerServer server, Socket socket) {
        this.server = server;
        this.socket = socket;
    }

    @Override
    public void run() {
        try {
            socket.setSoTimeout(15000);
            BufferedInputStream in = new BufferedInputStream(socket.getInputStream(), 8192);
            OutputStream out = socket.getOutputStream();

            // 1. Потоковое чтение заголовков до \r\n\r\n
            ByteArrayOutputStream headerBuffer = new ByteArrayOutputStream();
            int b;
            int pattern = 0;
            while ((b = in.read()) != -1) {
                headerBuffer.write(b);
                if ((pattern == 0 || pattern == 2) && b == '\r') {
                    pattern++;
                } else if ((pattern == 1 || pattern == 3) && b == '\n') {
                    pattern++;
                    if (pattern == 4) break;
                } else {
                    pattern = (b == '\r') ? 1 : 0;
                }
            }
            if (pattern != 4) return;

            String headerText = new String(headerBuffer.toByteArray(), StandardCharsets.US_ASCII);
            String[] lines = headerText.split("\r\n");
            if (lines.length == 0) return;

            String requestLine = lines[0];
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) return;
            String method = parts[0].toUpperCase(Locale.US);
            String fullPath = parts[1];

            Map<String, String> headers = new HashMap<>();
            int contentLength = 0;
            String contentType = "";
            for (int i = 1; i < lines.length; i++) {
                String line = lines[i];
                int colon = line.indexOf(':');
                if (colon > 0) {
                    String k = line.substring(0, colon).trim().toLowerCase(Locale.US);
                    String v = line.substring(colon + 1).trim();
                    headers.put(k, v);
                    if (k.equals("content-length")) {
                        try { contentLength = Integer.parseInt(v); } catch (Exception ignored) {}
                    } else if (k.equals("content-type")) {
                        contentType = v;
                    }
                }
            }

            // CORS preflight
            if (method.equals("OPTIONS")) {
                sendHeaders(out, 204, "No Content", "text/plain", 0);
                return;
            }

            if (contentLength > InformerServer.MAX_UPLOAD_BYTES) {
                sendResponse(out, 413, "Payload Too Large", "application/json",
                        "{\"error\":\"Payload exceeds 5MB limit\"}".getBytes(StandardCharsets.UTF_8));
                return;
            }

            // 2. Чтение тела запроса из того же BufferedInputStream
            byte[] body = new byte[contentLength];
            if (contentLength > 0) {
                int totalRead = 0;
                while (totalRead < contentLength) {
                    int count = in.read(body, totalRead, contentLength - totalRead);
                    if (count == -1) break;
                    totalRead += count;
                }
            }

            String path = fullPath;
            String query = "";
            int qIdx = fullPath.indexOf('?');
            if (qIdx >= 0) {
                path = fullPath.substring(0, qIdx);
                query = fullPath.substring(qIdx + 1);
            }

            handleRoute(method, path, query, headers, body, out, contentType);

        } catch (Exception e) {
            Log.e(TAG, "Request handling error: " + e.getMessage());
        } finally {
            try { socket.close(); } catch (IOException ignored) {}
        }
    }

    private void handleRoute(String method, String path, String query,
                             Map<String, String> headers, byte[] body,
                             OutputStream out, String contentType) throws Exception {

        // 1. Статус /health
        if (path.equals("/health")) {
            long uptime = (System.currentTimeMillis() - server.getStartTime()) / 1000;
            String json = "{\"status\":\"ok\",\"app\":\"WeatherInformerKiosk\",\"port\":" + InformerServer.PORT +
                    ",\"uptime_sec\":" + uptime + ",\"version\":\"1.0\"}";
            sendResponse(out, 200, "OK", "application/json; charset=utf-8", json.getBytes(StandardCharsets.UTF_8));
            return;
        }

        // 2. Страница настроек /settings
        if (path.equals("/settings")) {
            String html = server.getSettingsHtml();
            sendResponse(out, 200, "OK", "text/html; charset=utf-8", html.getBytes(StandardCharsets.UTF_8));
            return;
        }

        // 3. API настроек /api/settings
        if (path.equals("/api/settings")) {
            if (method.equals("GET")) {
                byte[] cfgBytes = server.readConfigFile();
                sendResponse(out, 200, "OK", "application/json; charset=utf-8", cfgBytes);
                return;
            } else if (method.equals("POST")) {
                File cfgFile = new File(server.getWebDir(), "config.json");
                try (FileOutputStream fos = new FileOutputStream(cfgFile)) {
                    fos.write(body);
                }
                if (server.getActivity() != null) {
                    server.getActivity().reloadInformer();
                }
                sendResponse(out, 200, "OK", "application/json; charset=utf-8",
                        "{\"status\":\"saved\"}".getBytes(StandardCharsets.UTF_8));
                return;
            }
        }

        // 4. Страница загрузки /upload
        if (path.equals("/upload")) {
            String html = server.getUploadHtml();
            sendResponse(out, 200, "OK", "text/html; charset=utf-8", html.getBytes(StandardCharsets.UTF_8));
            return;
        }

        // 5. API загрузки файлов /api/upload
        if (path.equals("/api/upload") && method.equals("POST")) {
            handleFileUpload(body, query, out, contentType);
            return;
        }

        // 6. Перезагрузка информера /api/reload
        if (path.equals("/api/reload")) {
            if (server.getActivity() != null) {
                server.getActivity().reloadInformer();
            }
            sendResponse(out, 200, "OK", "application/json; charset=utf-8",
                    "{\"status\":\"reloaded\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }

        // 7. CORS Прокси /proxy/weather
        if (path.equals("/proxy/weather")) {
            handleProxy(query, out);
            return;
        }

        // 8. Статические файлы (информер, js, css, config)
        if (path.equals("/") || path.equals("/informer.html") || path.equals("/index.html")) {
            serveFile("informer.html", "text/html; charset=utf-8", out);
            return;
        }

        String filename = path.startsWith("/") ? path.substring(1) : path;
        String mime = getMimeType(filename);
        serveFile(filename, mime, out);
    }

    private void serveFile(String filename, String mime, OutputStream out) throws IOException {
        filename = new File(filename).getName();

        File localFile = new File(server.getWebDir(), filename);
        if (localFile.exists() && localFile.isFile()) {
            byte[] data = readFileBytes(localFile);
            sendResponse(out, 200, "OK", mime, data);
            return;
        }

        try (InputStream is = server.getContext().getAssets().open(filename)) {
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) != -1) baos.write(buf, 0, n);
            sendResponse(out, 200, "OK", mime, baos.toByteArray());
        } catch (FileNotFoundException e) {
            sendResponse(out, 404, "Not Found", "text/plain", "File not found".getBytes(StandardCharsets.UTF_8));
        }
    }

    private void handleProxy(String query, OutputStream out) {
        Map<String, String> params = parseQuery(query);
        String targetUrl = params.get("url");
        String provider = params.get("provider");

        if (provider != null) {
            String lat = params.get("lat");
            String lon = params.get("lon");
            if (lat == null) lat = "56.3177";
            if (lon == null) lon = "43.9993";

            if (provider.equals("gismeteo")) {
                String token = params.get("token");
                if (token == null || token.isEmpty()) {
                    sendResponse(out, 400, "Bad Request", "application/json",
                            "{\"error\":\"token required for gismeteo\"}".getBytes(StandardCharsets.UTF_8));
                    return;
                }
                targetUrl = "https://api.gismeteo.net/v2/weather/forecast/aggregate/?latitude=" + lat + "&longitude=" + lon + "&days=3";
            } else if (provider.equals("openmeteo")) {
                targetUrl = "https://api.open-meteo.com/v1/forecast?latitude=" + lat + "&longitude=" + lon + "&hourly=temperature_2m,precipitation_probability,weathercode&current_weather=true";
            }
        }

        if (targetUrl == null) {
            sendResponse(out, 400, "Bad Request", "application/json",
                    "{\"error\":\"url or provider parameter required\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }

        try {
            URI uri = new URI(targetUrl);
            String host = uri.getHost();
            if (host == null || (!host.equals("api.gismeteo.net") &&
                                 !host.equals("api.weather.yandex.ru") &&
                                 !host.equals("api.open-meteo.com"))) {
                sendResponse(out, 403, "Forbidden", "application/json",
                        "{\"error\":\"Domain not allowed in proxy whitelist\"}".getBytes(StandardCharsets.UTF_8));
                return;
            }

            HttpURLConnection conn = (HttpURLConnection) uri.toURL().openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(15000);
            conn.setRequestProperty("User-Agent", "WeatherInformerLocal-TabletProxy/1.0");

            String token = params.get("token");
            if (token != null && !token.isEmpty()) {
                conn.setRequestProperty("X-Gismeteo-Token", token);
            }

            int code = conn.getResponseCode();
            InputStream stream = (code >= 200 && code < 400) ? conn.getInputStream() : conn.getErrorStream();
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            if (stream != null) {
                byte[] buf = new byte[4096];
                int n;
                while ((n = stream.read(buf)) != -1) baos.write(buf, 0, n);
            }
            sendResponse(out, code, conn.getResponseMessage(), "application/json; charset=utf-8", baos.toByteArray());

        } catch (Exception e) {
            sendResponse(out, 502, "Bad Gateway", "application/json",
                    ("{\"error\":\"Proxy request failed: " + e.getMessage() + "\"}").getBytes(StandardCharsets.UTF_8));
        }
    }

    private void handleFileUpload(byte[] body, String query, OutputStream out, String contentType) {
        try {
            String boundary = "";
            if (contentType != null && contentType.contains("boundary=")) {
                boundary = contentType.substring(contentType.indexOf("boundary=") + 9).trim();
                if (boundary.startsWith("\"") && boundary.endsWith("\"") && boundary.length() > 1) {
                    boundary = boundary.substring(1, boundary.length() - 1);
                }
            }

            String filename = "uploaded_file.bin";
            byte[] fileContent = body;

            if (!boundary.isEmpty()) {
                String bodyStr = new String(body, StandardCharsets.ISO_8859_1);
                String fileMarker = "filename=\"";
                int fIdx = bodyStr.indexOf(fileMarker);
                if (fIdx >= 0) {
                    int fEnd = bodyStr.indexOf("\"", fIdx + fileMarker.length());
                    if (fEnd > fIdx) {
                        filename = bodyStr.substring(fIdx + fileMarker.length(), fEnd);
                    }
                }

                int headerEnd = bodyStr.indexOf("\r\n\r\n");
                if (headerEnd >= 0) {
                    int dataStart = headerEnd + 4;
                    int dataEnd = bodyStr.indexOf("--" + boundary, dataStart);
                    if (dataEnd > dataStart) {
                        if (dataEnd >= 2 && bodyStr.charAt(dataEnd - 2) == '\r' && bodyStr.charAt(dataEnd - 1) == '\n') {
                            dataEnd -= 2;
                        }
                        fileContent = Arrays.copyOfRange(body, dataStart, dataEnd);
                    }
                }
            } else {
                Map<String, String> params = parseQuery(query);
                String qFile = params.get("file");
                if (qFile != null && !qFile.isEmpty()) {
                    filename = qFile;
                }
            }

            filename = new File(filename).getName();
            String lower = filename.toLowerCase(Locale.US);
            if (!lower.endsWith(".html") && !lower.endsWith(".js") && !lower.endsWith(".css") &&
                !lower.endsWith(".json") && !lower.endsWith(".png") && !lower.endsWith(".svg")) {
                sendResponse(out, 400, "Bad Request", "application/json",
                        "{\"error\":\"Only .html, .js, .css, .json, .png, .svg allowed\"}".getBytes(StandardCharsets.UTF_8));
                return;
            }

            File dest = new File(server.getWebDir(), filename);
            if (!dest.getCanonicalPath().startsWith(server.getWebDir().getCanonicalPath())) {
                sendResponse(out, 403, "Forbidden", "application/json",
                        "{\"error\":\"Path traversal rejected\"}".getBytes(StandardCharsets.UTF_8));
                return;
            }

            try (FileOutputStream fos = new FileOutputStream(dest)) {
                fos.write(fileContent);
            }

            if (server.getActivity() != null) {
                server.getActivity().reloadInformer();
            }

            sendResponse(out, 200, "OK", "application/json; charset=utf-8",
                    ("{\"status\":\"uploaded\",\"file\":\"" + filename + "\",\"size\":" + fileContent.length + "}").getBytes(StandardCharsets.UTF_8));

        } catch (Exception e) {
            sendResponse(out, 500, "Server Error", "application/json",
                    ("{\"error\":\"Upload error: " + e.getMessage() + "\"}").getBytes(StandardCharsets.UTF_8));
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

    private static void sendResponse(OutputStream out, int code, String msg, String contentType, byte[] data) {
        try {
            sendHeaders(out, code, msg, contentType, data.length);
            out.write(data);
            out.flush();
        } catch (IOException ignored) {}
    }

    private static void sendHeaders(OutputStream out, int code, String msg, String contentType, int length) throws IOException {
        String sb = "HTTP/1.1 " + code + " " + msg + "\r\n" +
                "Server: WeatherInformer-AndroidServer/1.0\r\n" +
                "Content-Type: " + contentType + "\r\n" +
                "Content-Length: " + length + "\r\n" +
                "Access-Control-Allow-Origin: *\r\n" +
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n" +
                "Access-Control-Allow-Headers: *\r\n" +
                "Connection: close\r\n\r\n";
        out.write(sb.getBytes(StandardCharsets.UTF_8));
    }

    private static String getMimeType(String name) {
        String n = name.toLowerCase(Locale.US);
        if (n.endsWith(".html")) return "text/html; charset=utf-8";
        if (n.endsWith(".js")) return "application/javascript; charset=utf-8";
        if (n.endsWith(".css")) return "text/css; charset=utf-8";
        if (n.endsWith(".json")) return "application/json; charset=utf-8";
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".svg")) return "image/svg+xml";
        if (n.endsWith(".ico")) return "image/x-icon";
        return "application/octet-stream";
    }

    private static Map<String, String> parseQuery(String query) {
        Map<String, String> map = new HashMap<>();
        if (query == null || query.isEmpty()) return map;
        for (String param : query.split("&")) {
            int eq = param.indexOf('=');
            if (eq > 0) {
                try {
                    String k = URLDecoder.decode(param.substring(0, eq), "UTF-8");
                    String v = URLDecoder.decode(param.substring(eq + 1), "UTF-8");
                    map.put(k, v);
                } catch (UnsupportedEncodingException ignored) {}
            }
        }
        return map;
    }
}
