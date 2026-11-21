package ru.weather.informer;

import android.util.Log;

import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;

/**
 * Минимальный RFC6455 WebSocket-клиент (stdlib, без внешних зависимостей).
 * Сигналинг камеры фазы 1: соединение к caching_server /ws/cam.
 * Только текстовые кадры, лимит размера, masking клиент->сервер, ping/pong, close.
 * java.net.http на API < 34 недоступен, поэтому сокет-реализация.
 */
public class CamSignalingClient {
    private static final String TAG = "CamSignalingClient";
    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int MAX_FRAME_BYTES = 128 * 1024;
    private static final int CONNECT_TIMEOUT_MS = 6000;

    /** Обратные вызовы; вызываются из reader-потока (не UI). */
    public interface Listener {
        void onOpen();
        void onText(String message);
        void onClosed(String reason);
    }

    private final URI uri;
    private final Listener listener;
    private final SecureRandom random = new SecureRandom();

    private Socket socket;
    private OutputStream out;
    private volatile boolean wantConnected = false;
    private volatile boolean socketOpen = false;
    private Thread readerThread;
    private final Object sendLock = new Object();

    public CamSignalingClient(URI uri, Listener listener) {
        this.uri = uri;
        this.listener = listener;
    }

    public boolean isOpen() {
        return socketOpen;
    }

    /** Открыть соединение (blocking). Вызывать из рабочего потока. */
    public void connect() {
        wantConnected = true;
        runSession();
    }

    /** Инициировать закрытие (не ждать). */
    public void disconnect(String reason) {
        wantConnected = false;
        sendClose(1000, reason);
        closeSocket();
    }

    /** Отправить текстовый кадр (masked). Потокобезопасно. */
    public boolean send(String text) {
        if (!socketOpen || out == null) {
            return false;
        }
        byte[] payload = text.getBytes(StandardCharsets.UTF_8);
        if (payload.length > MAX_FRAME_BYTES) {
            Log.w(TAG, "Frame too large: " + payload.length);
            return false;
        }
        synchronized (sendLock) {
            try {
                byte[] mask = new byte[4];
                random.nextBytes(mask);
                byte[] masked = new byte[payload.length];
                for (int i = 0; i < payload.length; i++) {
                    masked[i] = (byte) (payload[i] ^ mask[i & 3]);
                }
                out.write(0x81); // FIN + text
                int len = masked.length;
                if (len < 126) {
                    out.write(0x80 | len);
                } else if (len < 65536) {
                    out.write(0x80 | 126);
                    out.write((len >> 8) & 0xFF);
                    out.write(len & 0xFF);
                } else {
                    out.write(0x80 | 127);
                    for (int shift = 56; shift >= 0; shift -= 8) {
                        out.write((len >> shift) & 0xFF);
                    }
                }
                out.write(mask);
                out.write(masked);
                out.flush();
                return true;
            } catch (IOException e) {
                Log.w(TAG, "send failed: " + e.getMessage());
                closeSocket();
                notifyClosed("send failed");
                return false;
            }
        }
    }

    private void runSession() {
        try {
            int port = uri.getPort() > 0 ? uri.getPort() : 80;
            socket = new Socket();
            socket.connect(new InetSocketAddress(uri.getHost(), port), CONNECT_TIMEOUT_MS);
            socket.setSoTimeout(30000);
            out = socket.getOutputStream();
            String path = uri.getPath() == null || uri.getPath().isEmpty() ? "/" : uri.getPath();
            String query = uri.getQuery() != null ? "?" + uri.getQuery() : "";
            byte[] keyBytes = new byte[16];
            random.nextBytes(keyBytes);
            String wsKey = Base64.getEncoder().encodeToString(keyBytes);
            String handshake = "GET " + path + query + " HTTP/1.1\r\n"
                    + "Host: " + uri.getHost() + ":" + port + "\r\n"
                    + "Upgrade: websocket\r\n"
                    + "Connection: Upgrade\r\n"
                    + "Sec-WebSocket-Key: " + wsKey + "\r\n"
                    + "Sec-WebSocket-Version: 13\r\n\r\n";
            out.write(handshake.getBytes(StandardCharsets.US_ASCII));
            out.flush();

            BufferedInputStream in = new BufferedInputStream(socket.getInputStream(), 8192);
            String responseStatus = readHttpLine(in);
            if (responseStatus == null || !responseStatus.contains("101")) {
                closeSocket();
                notifyClosed("handshake rejected: " + responseStatus);
                return;
            }
            String line;
            boolean acceptOk = false;
            while ((line = readHttpLine(in)) != null && !line.isEmpty()) {
                int idx = line.indexOf(':');
                if (idx > 0) {
                    String name = line.substring(0, idx).trim();
                    String value = line.substring(idx + 1).trim();
                    if ("Sec-WebSocket-Accept".equalsIgnoreCase(name)) {
                        acceptOk = value.equals(expectedAccept(wsKey));
                    }
                }
            }
            if (!acceptOk) {
                closeSocket();
                notifyClosed("handshake: bad Sec-WebSocket-Accept");
                return;
            }

            socketOpen = true;
            try {
                listener.onOpen();
            } catch (Exception e) {
                Log.e(TAG, "listener.onOpen: " + e);
            }
            readFrames(in);
        } catch (Exception e) {
            Log.w(TAG, "connect failed: " + e.getMessage());
        } finally {
            boolean wasOpen = socketOpen;
            closeSocket();
            if (wasOpen) {
                notifyClosed("socket closed");
            } else {
                notifyClosed("connect failed");
            }
        }
    }

    private void readFrames(InputStream in) throws IOException {
        while (socketOpen) {
            int b0 = in.read();
            if (b0 < 0) {
                return;
            }
            int b1 = in.read();
            if (b1 < 0) {
                return;
            }
            boolean masked = (b1 & 0x80) != 0;
            long length = b1 & 0x7F;
            if (length == 126) {
                int hi = in.read();
                int lo = in.read();
                if (hi < 0 || lo < 0) return;
                length = ((hi & 0xFF) << 8) | (lo & 0xFF);
            } else if (length == 127) {
                length = 0;
                for (int i = 0; i < 8; i++) {
                    int v = in.read();
                    if (v < 0) return;
                    length = (length << 8) | (v & 0xFF);
                }
            }
            if (length > MAX_FRAME_BYTES) {
                Log.w(TAG, "frame too large: " + length);
                return;
            }
            byte[] maskKey = new byte[4];
            if (masked) {
                if (in.read(maskKey) != 4) return;
            }
            byte[] payload = new byte[(int) length];
            int total = 0;
            while (total < length) {
                int n = in.read(payload, total, (int) length - total);
                if (n < 0) return;
                total += n;
            }
            if (masked) {
                for (int i = 0; i < payload.length; i++) {
                    payload[i] = (byte) (payload[i] ^ maskKey[i & 3]);
                }
            }
            int opcode = b0 & 0x0F;
            if (opcode == 0x1) { // text
                String text = new String(payload, StandardCharsets.UTF_8);
                try {
                    listener.onText(text);
                } catch (Exception e) {
                    Log.e(TAG, "listener.onText: " + e);
                }
            } else if (opcode == 0x9) { // ping -> pong
                sendControl(0xA, payload);
            } else if (opcode == 0x8) { // close
                return;
            }
            // 0xA pong и фрагменты (0x0) игнорируются: JSON-кадры всегда короткие
        }
    }

    private void sendControl(int opcode, byte[] payload) {
        synchronized (sendLock) {
            try {
                byte[] mask = new byte[4];
                random.nextBytes(mask);
                out.write(0x80 | opcode);
                if (payload == null) payload = new byte[0];
                out.write(0x80 | payload.length);
                out.write(mask);
                for (int i = 0; i < payload.length; i++) {
                    out.write(payload[i] ^ mask[i & 3]);
                }
                out.flush();
            } catch (IOException e) {
                Log.w(TAG, "control frame failed: " + e.getMessage());
            }
        }
    }

    private void sendClose(int code, String reason) {
        String body = code + " " + (reason == null ? "" : reason);
        sendControl(0x8, body.getBytes(StandardCharsets.UTF_8));
    }

    private void closeSocket() {
        socketOpen = false;
        try {
            if (socket != null) {
                socket.close();
            }
        } catch (IOException ignored) {
        }
        socket = null;
        out = null;
    }

    private void notifyClosed(String reason) {
        try {
            listener.onClosed(reason);
        } catch (Exception e) {
            Log.e(TAG, "listener.onClosed: " + e);
        }
    }

    private static String expectedAccept(String wsKey) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] digest = md.digest((wsKey + WS_GUID).getBytes(StandardCharsets.US_ASCII));
            return Base64.getEncoder().encodeToString(digest);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String readHttpLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int prev = -1;
        int b;
        while ((b = in.read()) != -1) {
            if (prev == '\r' && b == '\n') {
                int len = sb.length();
                return sb.substring(0, Math.max(0, len - 1));
            }
            sb.append((char) b);
            prev = b;
        }
        return sb.length() > 0 ? sb.toString() : null;
    }
}
