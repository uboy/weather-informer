package ru.weather.informer;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.SurfaceTexture;
import android.graphics.SurfaceTexture;
import android.graphics.YuvImage;
import android.hardware.Camera;
import android.opengl.EGL14;
import android.opengl.EGLConfig;
import android.opengl.EGLContext;
import android.opengl.EGLDisplay;
import android.opengl.GLES20;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;
import org.webrtc.AudioSource;
import org.webrtc.AudioTrack;
import org.webrtc.Camera1Enumerator;
import org.webrtc.CameraVideoCapturer;
import org.webrtc.DefaultVideoDecoderFactory;
import org.webrtc.DefaultVideoEncoderFactory;
import org.webrtc.EglBase;
import org.webrtc.MediaConstraints;
import org.webrtc.PeerConnectionFactory;
import org.webrtc.SurfaceTextureHelper;
import org.webrtc.VideoSource;
import org.webrtc.VideoTrack;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Камерная подсистема приложения (фаза 1, вариант 2 - native org.webrtc).
 * State-machine IDLE/SNAPSHOT/STREAMING, арбитр владения камерой, локальный флаг
 * enabled (по умолчанию FALSE; меняется ТОЛЬКО подтверждением тапом на экране
 * планшета - решение владельца 5.1.6). WS-сигналинг на caching_server /ws/cam.
 * Точки входа для HTTP-маршрутов: getSnapshot (снапшот), requestToggle (оверлей),
 * buildStateJson (состояние). Включение камеры через сеть невозможно.
 */
public class CameraService {
    private static final String TAG = "CameraService";
    public static final String STATE_IDLE = "IDLE";
    public static final String STATE_SNAPSHOT = "SNAPSHOT";
    public static final String STATE_STREAMING = "STREAMING";

    public static final int SNAP_OK = 0;
    public static final int SNAP_FORBIDDEN_DISABLED = 1;
    public static final int SNAP_FORBIDDEN_TOKEN = 2;
    public static final int SNAP_BUSY = 3;
    public static final int SNAP_ERROR = 4;

    public static final int PULL_OK = 0;
    public static final int PULL_FORBIDDEN_DISABLED = 1;
    public static final int PULL_TIMEOUT = 2;
    public static final int PULL_ERROR = 3;
    public static final int PULL_FORBIDDEN_TOKEN = 4;

    private static volatile CameraService instance;
    private static final Object INSTANCE_LOCK = new Object();

    private final Context appContext;
    private final HandlerThread camThread;
    private final Handler camHandler;
    private final Handler mainHandler;
    private final Object sessionsLock = new Object();
    /** Сериализует сами съёмки снапшота (арбитр состояния - на this, короткий). */
    private final Object snapshotLock = new Object();
    private final Map<String, CamPeerSession> sessions = new LinkedHashMap<String, CamPeerSession>();
    private final AtomicInteger reconnectAttempt = new AtomicInteger(0);

    private SharedPreferences prefs;
    private volatile String state = STATE_IDLE;
    private volatile String lastError = "";
    private volatile long stateTs = System.currentTimeMillis() / 1000L;

    private PeerConnectionFactory factory;
    private EglBase eglBase;
    private SurfaceTextureHelper surfaceHelper;
    private VideoSource videoSource;
    private VideoTrack videoTrack;
    private AudioSource audioSource;
    private AudioTrack audioTrack;
    private CameraVideoCapturer capturer;

    private CamSignalingClient signaling;
    private volatile boolean signalingConnected = false;

    public static CameraService getInstance() {
        return instance;
    }

    public static void init(Context appContext) {
        synchronized (INSTANCE_LOCK) {
            if (instance == null) {
                instance = new CameraService(appContext);
            }
        }
    }

    private CameraService(Context appContext) {
        this.appContext = appContext;
        this.prefs = appContext.getSharedPreferences("cam", Context.MODE_PRIVATE);
        this.camThread = new HandlerThread("CamServiceThread");
        this.camThread.start();
        this.camHandler = new Handler(camThread.getLooper());
        this.mainHandler = new Handler(Looper.getMainLooper());
        if (isEnabled()) {
            // камера включена до рестарта приложения - восстановить сигналинг
            camHandler.post(new CamConnectTask(this));
        }
    }

    // ===== Состояние и флаг enabled =====

    public synchronized boolean isEnabled() {
        return prefs.getBoolean("enabled", false);
    }

    /**
     * Единственная точка изменения флага. Вызывается ТОЛЬКО из MainActivity
     * после подтверждения тапом на экране планшета. Сетевые маршруты сюда не доходят.
     */
    public synchronized void setEnabled(boolean enabled) {
        boolean old = isEnabled();
        if (old == enabled) {
            return;
        }
        prefs.edit().putBoolean("enabled", enabled).apply();
        Log.i(TAG, "enabled " + old + " -> " + enabled + " (local confirm)");
        if (enabled) {
            setState(STATE_IDLE, "");
            camHandler.post(new CamEnableTask(this));
        } else {
            camHandler.post(new CamDisableTask(this));
        }
    }

    public synchronized String getState() {
        return state;
    }

    private synchronized void setState(String newState, String error) {
        this.state = newState;
        this.stateTs = System.currentTimeMillis() / 1000L;
        if (error != null && !error.isEmpty()) {
            this.lastError = error;
        }
        sendStateUpdate();
    }

    private static void jput(JSONObject o, String key, Object value) {
        try {
            o.put(key, value);
        } catch (Exception e) {
            throw new IllegalStateException("json put failed: " + key, e);
        }
    }

    public JSONObject buildStateJson() {
        JSONObject o = new JSONObject();
        jput(o, "enabled", isEnabled());
        // Какой device index будет использован (-1 = авто front-first);
        // верификация выбора камеры снаружи, без logcat.
        jput(o, "camera_id", camDeviceOverride());
        jput(o, "exp_profile", camExpProfile());
        jput(o, "state", state);
        jput(o, "last_error", lastError == null ? "" : lastError);
        jput(o, "viewers", sessionsCount());
        jput(o, "ts", stateTs);
        return o;
    }

    private int sessionsCount() {
        synchronized (sessionsLock) {
            return sessions.size();
        }
    }

    // ===== Конфиг камеры с планшета (web/config.json, секция cam) =====

    /** Секция cam конфига планшета как есть (без требований к ws_url/room). */
    private JSONObject camSection() {
        try {
            InformerServer server = InformerServer.getInstance();
            if (server == null) {
                return null;
            }
            byte[] cfgBytes = server.readConfigFile();
            JSONObject cfg = new JSONObject(new String(cfgBytes, java.nio.charset.StandardCharsets.UTF_8));
            return cfg.optJSONObject("cam");
        } catch (Exception e) {
            Log.w(TAG, "camSection: " + e.getMessage());
            return null;
        }
    }

    /** Для сигналинга: секция cam только если publish настроен (ws_url и room). */
    private JSONObject getCamConfig() {
        JSONObject cam = camSection();
        if (cam == null) {
            return null;
        }
        String ws = cam.optString("ws_url", "");
        String room = cam.optString("room", "");
        if (ws.isEmpty() || room.isEmpty()) {
            return null;
        }
        return cam;
    }

    /** cam.camera_id из конфига планшета (override device index); -1 = авто (front-first).
     *  Читается напрямую из секции cam: override не зависит от ws_url/room
     *  (pull-модель D4: ws_url умирает, override остаётся). */
    private int camDeviceOverride() {
        JSONObject cam = camSection();
        if (cam == null) {
            return -1;
        }
        int id = cam.optInt("camera_id", -1);
        return id >= 0 ? id : -1;
    }

    /** Профиль экспозиции (cam.exp_profile, 0 = поведение по умолчанию).
     * Диагностика чёрных дневных кадров T74HMi (.5): живой кадр вечером при
     * свете, чёрный днём на обоих индексах и обоих бинарях - подозрение
     * AE/ISP при высокой освещённости. Переключается POST /api/settings. */
    private int camExpProfile() {
        JSONObject cam = camSection();
        if (cam == null) {
            return 0;
        }
        return cam.optInt("exp_profile", 0);
    }

    // ===== Сигналинг =====

    private void connectSignaling() {
        if (!isEnabled()) {
            return;
        }
        JSONObject cam = getCamConfig();
        if (cam == null) {
            // D4 pull-модель: ws_url/room не заданы - publish ВЫКЛЮЧЕН по дизайну.
            // Это не ошибка и не повод для исходящих попыток.
            setLastError("");
            Log.i(TAG, "publish disabled (cam.ws_url/room not set), D4 pull-model");
            return;
        }
        // Конфиг прочитан успешно: транзиентная ошибка старта (файл ещё не был
        // готов) устарела, иначе health показывает её вечно.
        clearLastErrorIf("cam config (ws_url/room) not set");
        if (signaling != null && signaling.isOpen()) {
            return;
        }
        try {
            URI uri = new URI(cam.optString("ws_url", ""));
            String token = cam.optString("token", "");
            String room = cam.optString("room", "");
            CamSignalingClient client = new CamSignalingClient(uri, new CamSignalingListener(this, token, room));
            signaling = client;
            Thread t = new Thread(new CamConnectRunner(this), "CamSignalingConnect");
            t.setDaemon(true);
            t.start();
        } catch (Exception e) {
            setLastError("bad ws_url: " + e.getMessage());
            scheduleReconnect();
        }
    }

    /** D4: publish-петля снесена - ограниченный бюджет попыток, не вечный цикл. */
    private static final int MAX_RECONNECT_ATTEMPTS = 5;

    private void scheduleReconnect() {
        if (!isEnabled()) {
            return;
        }
        int attempt = reconnectAttempt.incrementAndGet();
        if (attempt > MAX_RECONNECT_ATTEMPTS) {
            setLastError("publish endpoint unreachable after " + MAX_RECONNECT_ATTEMPTS
                    + " attempts; повтор при включении камеры, сохранении конфига или перезапуске");
            Log.w(TAG, "reconnect limit reached, publish attempts stopped (D4)");
            return;
        }
        long delayMs = Math.min(30000L, 2000L * attempt);
        Log.i(TAG, "reconnect in " + delayMs + "ms (attempt " + attempt
                + "/" + MAX_RECONNECT_ATTEMPTS + ")");
        camHandler.postDelayed(new CamConnectTask(this), delayMs);
    }

    /** Листенер сигналинга (именованное вложение - гоча d8). */
    private static final class CamSignalingListener implements CamSignalingClient.Listener {
        private final CameraService service;
        private final String token;
        private final String room;

        CamSignalingListener(CameraService service, String token, String room) {
            this.service = service;
            this.token = token;
            this.room = room;
        }

        @Override
        public void onOpen() {
            service.signalingConnected = true;
            service.reconnectAttempt.set(0);
            service.setLastError("");
            JSONObject hello = new JSONObject();
            jput(hello, "type", "hello");
            jput(hello, "role", "publisher");
            jput(hello, "room", room);
            jput(hello, "token", token);
            service.sendSignaling(hello.toString());
            service.sendStateUpdate();
            Log.i(TAG, "signaling open, room=" + room);
        }

        @Override
        public void onText(String message) {
            service.handleSignalingMessage(message);
        }

        @Override
        public void onClosed(String reason) {
            service.signalingConnected = false;
            Log.w(TAG, "signaling closed: " + reason);
            service.camHandler.post(new CamSignalingLostTask(service));
        }
    }

    private void handleSignalingMessage(String raw) {
        try {
            JSONObject msg = new JSONObject(raw);
            String type = msg.optString("type", "");
            camHandler.post(new CamDispatchTask(this, type, msg));
        } catch (Exception e) {
            Log.w(TAG, "bad signaling message: " + e.getMessage());
        }
    }

    /** На camThread. */
    private void dispatchSignaling(String type, JSONObject msg) {
        if (!isEnabled()) {
            return;
        }
        if ("offer".equals(type)) {
            String viewer = msg.optString("from", "");
            JSONObject payload = msg.optJSONObject("payload");
            if (viewer.isEmpty() || payload == null) {
                return;
            }
            try {
                ensureCapture();
            } catch (Exception e) {
                setLastError("capture start failed: " + e.getMessage());
                return;
            }
            CamPeerSession session = new CamPeerSession(viewer, this, new CamSignalingSink(this));
            synchronized (sessionsLock) {
                CamPeerSession prev = sessions.get(viewer);
                if (prev != null) {
                    prev.close("replaced by new offer");
                }
                sessions.put(viewer, session);
            }
            setState(STATE_STREAMING, "");
            session.handleOffer(payload);
        } else if ("ice".equals(type)) {
            String viewer = msg.optString("from", "");
            JSONObject payload = msg.optJSONObject("payload");
            CamPeerSession session = null;
            synchronized (sessionsLock) {
                session = sessions.get(viewer);
            }
            if (session != null && payload != null) {
                session.handleRemoteIce(payload);
            }
        } else if ("viewer_leave".equals(type)) {
            dropSession(msg.optString("viewer", ""), "viewer_leave");
        } else if ("stop".equals(type)) {
            closeAllSessions("server stop");
            stopCapture();
            setStateIdleIfNoSession();
        } else if ("ping_app".equals(type)) {
            JSONObject pong = new JSONObject();
            jput(pong, "type", "pong_app");
            sendSignaling(pong.toString());
        }
    }

    private boolean sendSignaling(String text) {
        CamSignalingClient client = signaling;
        if (client == null || !client.isOpen()) {
            return false;
        }
        return client.send(text);
    }

    private void sendStateUpdate() {
        JSONObject msg = new JSONObject();
        jput(msg, "type", "cam_state");
        jput(msg, "state", state);
        jput(msg, "enabled", isEnabled());
        jput(msg, "ts", stateTs);
        sendSignaling(msg.toString());
    }

    private void setLastError(String error) {
        this.lastError = error == null ? "" : error;
        Log.w(TAG, "lastError: " + error);
    }

    /** Ошибку стираем, когда её условие разрешилось (стартовая гонка и т.п.). */
    private void clearLastErrorIf(String prefix) {
        if (lastError != null && lastError.startsWith(prefix)) {
            lastError = "";
            Log.i(TAG, "lastError cleared, resolved: " + prefix);
            sendStateUpdate();
        }
    }

    // ===== Колбэки сессий =====

    /** Sink сообщений к зрителю (именованное вложение). */
    private static final class CamSignalingSink implements CamPeerSession.SignalingSink {
        private final CameraService service;

        CamSignalingSink(CameraService service) {
            this.service = service;
        }

        @Override
        public boolean sendToViewer(String viewerId, JSONObject message) {
            jput(message, "to", viewerId);
            return service.sendSignaling(message.toString());
        }
    }

    void dropSession(String viewerId, String reason) {
        camHandler.post(new CamDropSessionTask(this, viewerId, reason));
    }

    private void dropSessionOnThread(String viewerId, String reason) {
        boolean empty;
        synchronized (sessionsLock) {
            CamPeerSession session = sessions.remove(viewerId);
            if (session != null) {
                session.close(reason);
            }
            empty = sessions.isEmpty();
        }
        if (empty) {
            stopCapture();
            setStateIdleIfNoSession();
        }
    }

    void notifySessionFailed(String viewerId, String reason) {
        Log.w(TAG, "session " + viewerId + " failed: " + reason);
        dropSession(viewerId, reason);
    }

    // ===== Pull-сигналинг (сервер сам подключается к планшету, D4) =====

    /** Токен-гейт pull-маршрутов: тот же cam.token, что у снапшота.
     *  PULL_OK / PULL_FORBIDDEN_DISABLED / PULL_FORBIDDEN_TOKEN. */
    public int checkPullToken(String token) {
        if (!isEnabled()) {
            return PULL_FORBIDDEN_DISABLED;
        }
        JSONObject cam = camSection();
        String expected = cam == null ? "" : cam.optString("token", "");
        if (expected.isEmpty() || token == null || !expected.equals(token)) {
            return PULL_FORBIDDEN_TOKEN;
        }
        return PULL_OK;
    }

    /**
     * Синхронный pull-offer: сервер POST-ит offer зрителя, планшет отвечает
     * answer+ICE одним HTTP-ответом (non-trickle). Вызывается из потока
     * HTTP-обработчика; работа с камерой (ensureCapture/регистрация сессии)
     * идёт на camThread (CamPullOfferTask), ожидание - на latch сессии, без
     * удержания мониторов. Offer-путь совпадает с WS-сигналингом: та же
     * карта sessions, тот же арбитр состояния и stopCapture при уходе зрителей.
     */
    public int startPullSession(String viewerId, String token, JSONObject offerPayload,
                                boolean wantAudio, long timeoutMs, JSONObject[] answerOut) {
        int gate = checkPullToken(token);
        if (gate != PULL_OK) {
            return gate;
        }
        CamPeerSession session = new CamPeerSession(viewerId, this, null, wantAudio);
        camHandler.post(new CamPullOfferTask(this, viewerId, offerPayload, session));
        if (!session.waitForAnswerAndIce(timeoutMs)) {
            String err = session.pullError();
            dropSession(viewerId, err.isEmpty() ? "pull answer timeout" : "pull start failed: " + err);
            return err.isEmpty() ? PULL_TIMEOUT : PULL_ERROR;
        }
        answerOut[0] = session.buildAnswerJson();
        Log.i(TAG, "pull session ready for " + viewerId
                + " audio=" + wantAudio + " viewers=" + sessionsCount());
        return PULL_OK;
    }

    private void closeAllSessions(String reason) {
        synchronized (sessionsLock) {
            Iterator<Map.Entry<String, CamPeerSession>> it = sessions.entrySet().iterator();
            while (it.hasNext()) {
                it.next().getValue().close(reason);
                it.remove();
            }
        }
    }

    private synchronized void setStateIdleIfNoSession() {
        if (sessionsCount() == 0 && !STATE_IDLE.equals(state)) {
            setState(STATE_IDLE, "");
        }
    }

    // ===== Захват (общий на всех зрителей) =====

    PeerConnectionFactory getFactory() {
        return factory;
    }

    VideoTrack getVideoTrack() {
        return videoTrack;
    }

    AudioTrack getAudioTrack() {
        return audioTrack;
    }

    private synchronized void ensureCapture() throws Exception {
        if (factory == null) {
            initFactory();
        }
        if (capturer != null) {
            return;
        }
        Camera1Enumerator enumerator = new Camera1Enumerator(true);
        // cam.camera_id (опционально в конфиге планшета): явный индекс device.
        // Зачем: MTK HAL лжёт в CameraInfo.facing (AT200: единственный
        // ФРОНТАЛЬНЫЙ модуль помечен BACK), доверять facing нельзя. Override
        // приватность не ослабляет: включение камеры по-прежнему только тапом
        // на экране планшета; поле лишь указывает, какой device открывать.
        int overrideId = camDeviceOverride();
        String device = null;
        if (overrideId >= 0) {
            for (String name : enumerator.getDeviceNames()) {
                if (name.equals(String.valueOf(overrideId))) {
                    device = name;
                    break;
                }
            }
            if (device == null) {
                throw new IllegalStateException(
                        "cam.camera_id=" + overrideId + " not found in camera device list");
            }
        } else {
            for (String name : enumerator.getDeviceNames()) {
                if (enumerator.isFrontFacing(name)) {
                    device = name;
                    break;
                }
            }
            if (device == null) {
                throw new IllegalStateException("no front camera: front-only by policy 5.1.6");
            }
        }
        CamCaptureEvents events = new CamCaptureEvents(this);
        capturer = enumerator.createCapturer(device, events);
        surfaceHelper = SurfaceTextureHelper.create("CamCaptureThread", eglBase.getEglBaseContext());
        capturer.initialize(surfaceHelper, appContext, videoSource.getCapturerObserver());
        capturer.startCapture(640, 480, 15);
        Log.i(TAG, "capture started: device=" + device
                + (overrideId >= 0 ? " (override cam.camera_id)" : " (front)")
                + " 640x480@15");
    }

    /**
     * Остановить захват, если зрителей не осталось (число сессий - источник истины).
     */
    private synchronized void stopCapture() {
        if (sessionsCount() > 0 || capturer == null) {
            return;
        }
        try {
            capturer.stopCapture();
        } catch (Exception e) {
            Log.w(TAG, "stopCapture: " + e.getMessage());
        }
        try {
            capturer.dispose();
        } catch (Exception e) {
            Log.w(TAG, "capturer.dispose: " + e.getMessage());
        }
        capturer = null;
        if (surfaceHelper != null) {
            surfaceHelper.dispose();
            surfaceHelper = null;
        }
        Log.i(TAG, "capture stopped");
    }

    private synchronized void initFactory() {
        if (factory != null) {
            return;
        }
        PeerConnectionFactory.InitializationOptions initOptions =
                PeerConnectionFactory.InitializationOptions.builder(appContext)
                        .setEnableInternalTracer(false)
                        .createInitializationOptions();
        PeerConnectionFactory.initialize(initOptions);
        eglBase = EglBase.create();
        factory = PeerConnectionFactory.builder()
                .setVideoEncoderFactory(new DefaultVideoEncoderFactory(
                        eglBase.getEglBaseContext(), true, true))
                .setVideoDecoderFactory(new DefaultVideoDecoderFactory(eglBase.getEglBaseContext()))
                .createPeerConnectionFactory();
        videoSource = factory.createVideoSource(false);
        videoTrack = factory.createVideoTrack("cam_video_0", videoSource);
        try {
            MediaConstraints audioConstraints = new MediaConstraints();
            audioSource = factory.createAudioSource(audioConstraints);
            audioTrack = factory.createAudioTrack("cam_audio_0", audioSource);
            Log.i(TAG, "audio track created");
        } catch (Throwable t) {
            audioSource = null;
            audioTrack = null;
            setLastError("audio unavailable (HAL blocked?): " + t.getMessage());
            Log.w(TAG, "audio source failed, camera-only: " + t.getMessage());
        }
    }

    /** События захвата (именованное вложение). */
    private static final class CamCaptureEvents implements CameraVideoCapturer.CameraEventsHandler {
        private final CameraService service;

        CamCaptureEvents(CameraService service) {
            this.service = service;
        }

        @Override
        public void onCameraError(String errorDescription) {
            service.setLastError("camera error: " + errorDescription);
        }

        @Override
        public void onCameraDisconnected() {
            service.setLastError("camera disconnected");
        }

        @Override
        public void onCameraFreezed(String errorDescription) {
            service.setLastError("camera freezed: " + errorDescription);
        }

        @Override
        public void onCameraOpening(String cameraName) {
        }

        @Override
        public void onFirstFrameAvailable() {
        }

        @Override
        public void onCameraClosed() {
        }
    }

    // ===== Снапшот (Camera1 напрямую, вне WebRTC) =====

    /**
     * Снапшот JPEG фронтальной камерой. Арбитр: только из IDLE, STREAMING -> busy.
     * token - tablet_token из секции cam конфига планшета.
     * Возвращает SNAP_* код; jpegOut[0] = байты JPEG при SNAP_OK.
     */
    public int getSnapshot(String token, byte[][] jpegOut) {
        if (!isEnabled()) {
            return SNAP_FORBIDDEN_DISABLED;
        }
        JSONObject cam = camSection();
        String expected = cam == null ? "" : cam.optString("token", "");
        if (expected.isEmpty() || token == null || !expected.equals(token)) {
            return SNAP_FORBIDDEN_TOKEN;
        }
        // Арбитр: короткий замок this только на проверку/переключение состояния.
        // Раньше this держался на всю съёмку (~7.5с) и setEnabled с UI-потока
        // (MainActivity.finishCamConfirm) блокировался до конца съёмки -> ANR.
        // Сама съёмка сериализуется отдельным snapshotLock: одновременных
        // снапшотов нет; гонка с toggle в момент съёмки разрешается ошибкой
        // Camera.open на одной из сторон (graceful, не краш).
        synchronized (this) {
            if (!STATE_IDLE.equals(state)) {
                return SNAP_BUSY;
            }
            setState(STATE_SNAPSHOT, "");
        }
        try {
            synchronized (snapshotLock) {
                byte[] jpeg = captureJpeg();
                if (jpeg != null) {
                    clearLastErrorIf("snapshot failed");
                }
                jpegOut[0] = jpeg;
                return jpeg != null ? SNAP_OK : SNAP_ERROR;
            }
        } catch (Throwable t) {
            setLastError("snapshot failed: " + t.getMessage());
            return SNAP_ERROR;
        } finally {
            setStateIdleIfNoSession();
        }
    }

    /** Ниже этой средней яркости still-кадр считается чёрным (замеры T74HMi: 1.6-1.8). */
    private static final int BLACK_LUMA = 8;

    private byte[] captureJpeg() throws Exception {
        Camera.CameraInfo info = new Camera.CameraInfo();
        int cameraId = -1;
        int overrideId = camDeviceOverride();
        int n = Camera.getNumberOfCameras();
        if (overrideId >= 0) {
            // Явный override (см. ensureCapture): facing на MTK бывает лжив.
            if (overrideId >= n) {
                throw new IllegalStateException(
                        "cam.camera_id=" + overrideId + " out of range (" + n + " cameras)");
            }
            cameraId = overrideId;
        } else {
            for (int i = 0; i < n; i++) {
                Camera.getCameraInfo(i, info);
                if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) {
                    cameraId = i;
                    break;
                }
            }
            if (cameraId < 0) {
                throw new IllegalStateException("no front camera: front-only by policy 5.1.6");
            }
        }
        Camera.getCameraInfo(cameraId, info);
        Camera camera = Camera.open(cameraId);
        int expProfile = camExpProfile();
        try {
            SurfaceTexture dummy = new SurfaceTexture(0);
            SurfaceTexture realTex = null;
            if (expProfile >= 13) {
                // реальный GL-потребитель (профили 13/14): SurfaceTexture(0) - имя
                // текстуры 0 невалидно без GL-контекста; гипотеза: MTK гонит preview
                // без 3A через bypass-путь -> ночью сырой кадр светлый, днём чёрный.
                EGLDisplay dpy = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);
                int[] ver = new int[2];
                EGL14.eglInitialize(dpy, ver, 0, ver, 1);
                int[] attribs = {EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8,
                        EGL14.EGL_BLUE_SIZE, 8,
                        EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
                        EGL14.EGL_NONE};
                EGLConfig[] cfgs = new EGLConfig[1];
                int[] num = new int[1];
                EGL14.eglChooseConfig(dpy, attribs, 0, cfgs, 0, 1, num, 0);
                EGLContext ctx = EGL14.eglCreateContext(dpy, cfgs[0],
                        EGL14.EGL_NO_CONTEXT,
                        new int[]{EGL14.EGL_CONTEXT_CLIENT_VERSION, 2,
                                EGL14.EGL_NONE}, 0);
                EGL14.eglMakeCurrent(dpy, EGL14.EGL_NO_SURFACE,
                        EGL14.EGL_NO_SURFACE, ctx);
                int[] tex = new int[1];
                GLES20.glGenTextures(1, tex, 0);
                realTex = new SurfaceTexture(tex[0]);
                realTex.setDefaultBufferSize(640, 480);
                try {
                    camera.setPreviewTexture(realTex);
                    Log.i(TAG, "exp: real texture " + tex[0] + " attached");
                } catch (Exception e) {
                    Log.w(TAG, "setPreviewTexture real: " + e.getMessage());
                }
            } else {
                try {
                    camera.setPreviewTexture(dummy);
                } catch (Exception e) {
                    Log.w(TAG, "setPreviewTexture: " + e.getMessage());
                }
            }
            Camera.Parameters params = camera.getParameters();
            params.setJpegQuality(70);
            try {
                params.setPreviewSize(640, 480);
            } catch (Exception e) {
                try {
                    params.setPreviewSize(320, 240);
                } catch (Exception ignored) {
                }
            }
            applyExpProfile(params, expProfile);
            camera.setParameters(params);
            CamPictureCallback cb = new CamPictureCallback(this);
            final Object done = new Object();
            // AE-прогрев: мгновенный takePicture сразу после startPreview на холодном
            // MTK HAL возвращает чёрный JPEG (T74HMi: 1.8/255 на обоих camera_id,
            // hot+cold, сборка 21:11). Ждём реальные кадры preview, потом снимаем.
            CamPreviewWarmup warmup = new CamPreviewWarmup();
            camera.setPreviewCallback(warmup);
            camera.startPreview();
            CameraSnapshotWaiter.waitAndTake(camera, cb, done, warmup);
            byte[] still = cb.getJpeg();
            int stillLuma = jpegLuma(still);
            boolean stillBad = (still == null) || (stillLuma >= 0 && stillLuma < BLACK_LUMA);
            if (!stillBad) {
                Log.i(TAG, "snapshot: still used, luma=" + stillLuma + " bytes=" + still.length);
                return still;
            }
            // Still-путь на холодном MTK HAL отдаёт чёрный кадр (T74HMi: 1.6-1.8/255
            // на обоих camera_id hot+cold) при живом preview-конвейере - собираем
            // кадр из последнего preview-буфера.
            byte[] fb = previewFallbackJpeg(warmup,
                    info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT, info.orientation);
            if (fb != null) {
                Log.i(TAG, "snapshot: still black/none (luma=" + stillLuma
                        + "), preview fallback used, preview_luma=" + warmup.getLastLuma()
                        + " bytes=" + fb.length);
                return fb;
            }
            if (still != null) {
                Log.w(TAG, "snapshot: still black (luma=" + stillLuma
                        + "), no preview frame kept - returning black still");
                return still;
            }
            throw new IllegalStateException("takePicture timeout and no preview frame");
        } finally {
            try {
                camera.stopPreview();
            } catch (Exception ignored) {
            }
            camera.release();
            if (expProfile >= 13) {
                try {
                    EGL14.eglMakeCurrent(EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY),
                            EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE,
                            EGL14.EGL_NO_CONTEXT);
                } catch (Exception ignored) {
                }
            }
            Log.i(TAG, "snapshot camera released");
        }
    }

    /** Профили экспозиции диагностики чёрных дневных кадров (.5).
     * 0 - по умолчанию; 1 - анти-банддинг 50 Гц; 2 - компенсация MIN + AE-lock;
     * 3 - компенсация MAX + AE-lock; 4 - фикс fps-range (верхний) + анти-банддинг;
     * 5 - компенсация MAX + самый длинный fps-range + ночная сцена.
     * Каждый шаг в try/catch (неподдержанное - пропускаем) и в logcat. */
    private static void applyExpProfile(Camera.Parameters params, int profile) {
        Log.i(TAG, "exp profile=" + profile
                + " supported: comp=[" + params.getMinExposureCompensation() + ".."
                + params.getMaxExposureCompensation()
                + "] antibanding=" + params.getSupportedAntibanding()
                + " scenes=" + params.getSupportedSceneModes()
                + " fpsRanges=" + params.getSupportedPreviewFpsRange());
        if (profile == 0) {
            return;
        }
        if (profile == 1 || profile == 4) {
            try {
                params.setAntibanding(Camera.Parameters.ANTIBANDING_50HZ);
            } catch (Exception e) {
                Log.w(TAG, "exp: antibanding: " + e.getMessage());
            }
        }
        if (profile == 2 || profile == 3 || profile == 5 || profile == 11
                || profile == 12 || profile == 14 || profile == 15
                || profile == 16) {
            try {
                int target = profile == 2 ? params.getMinExposureCompensation()
                        : params.getMaxExposureCompensation();
                params.setExposureCompensation(target);
                params.setAutoExposureLock(true);
                Log.i(TAG, "exp: compensation=" + target + " aeLock=true");
            } catch (Exception e) {
                Log.w(TAG, "exp: compensation: " + e.getMessage());
            }
        }
        if (profile == 4) {
            try {
                List<int[]> ranges = params.getSupportedPreviewFpsRange();
                if (ranges != null && !ranges.isEmpty()) {
                    int[] r = ranges.get(ranges.size() - 1);
                    params.setPreviewFpsRange(r[0], r[1]);
                    Log.i(TAG, "exp: fpsRange=" + r[0] + ".." + r[1]);
                }
            } catch (Exception e) {
                Log.w(TAG, "exp: fpsRange: " + e.getMessage());
            }
        }
        if (profile == 6 || profile == 7 || profile == 8) {
            // зеркало родной камеры (dumpsys 11:10): preview 800x600 + scene hdr
            try {
                params.setPreviewSize(800, 600);
            } catch (Exception e) {
                Log.w(TAG, "exp: preview 800x600: " + e.getMessage());
            }
        }
        if (profile == 6) {
            try {
                params.setSceneMode(Camera.Parameters.SCENE_MODE_HDR);
                Log.i(TAG, "exp: scene=hdr");
            } catch (Exception e) {
                Log.w(TAG, "exp: scene hdr: " + e.getMessage());
            }
        }
        if (profile == 7) {
            try {
                params.setSceneMode(Camera.Parameters.SCENE_MODE_NIGHT);
                Log.i(TAG, "exp: scene=night");
            } catch (Exception e) {
                Log.w(TAG, "exp: scene night: " + e.getMessage());
            }
        }
        if (profile == 12) {
            try {
                params.set("iso-speed", "1600");
                params.set("brightness", "high");
                params.set("eng-mfll-e", "1");
                params.set("eng-mfll-s", "1");
                params.setAntibanding(Camera.Parameters.ANTIBANDING_50HZ);
                Log.i(TAG, "exp: combo iso1600+brightness+mfll+50hz");
            } catch (Exception e) {
                Log.w(TAG, "exp: combo: " + e.getMessage());
            }
        }
        if (profile == 15 || profile == 16) {
            try {
                params.set("cap-mode", profile == 15 ? "evbracketshot" : "off");
                Log.i(TAG, "exp: cap-mode=" + params.get("cap-mode"));
            } catch (Exception e) {
                Log.w(TAG, "exp: cap-mode: " + e.getMessage());
            }
        }
        if (profile == 9 || profile == 11) {
            // ручной ISO (iso-speed-values: auto,100,200,400,800,1600 из dumpsys)
            try {
                params.set("iso-speed", "1600");
                Log.i(TAG, "exp: iso=1600");
            } catch (Exception e) {
                Log.w(TAG, "exp: iso: " + e.getMessage());
            }
        }
        if (profile == 10) {
            // MTK-вендорская яркость (brightness-values: low,middle,high)
            try {
                params.set("brightness", "high");
                Log.i(TAG, "exp: brightness=high");
            } catch (Exception e) {
                Log.w(TAG, "exp: brightness: " + e.getMessage());
            }
        }
        if (profile == 5) {
            try {
                List<int[]> ranges = params.getSupportedPreviewFpsRange();
                if (ranges != null && !ranges.isEmpty()) {
                    int[] best = ranges.get(0);
                    for (int[] r : ranges) {
                        if (r[0] < best[0]) {
                            best = r;
                        }
                    }
                    params.setPreviewFpsRange(best[0], best[1]);
                    Log.i(TAG, "exp: long fpsRange=" + best[0] + ".." + best[1]);
                }
                List<String> scenes = params.getSupportedSceneModes();
                if (scenes != null && scenes.contains(Camera.Parameters.SCENE_MODE_NIGHT)) {
                    params.setSceneMode(Camera.Parameters.SCENE_MODE_NIGHT);
                    Log.i(TAG, "exp: scene=night");
                }
            } catch (Exception e) {
                Log.w(TAG, "exp: night/long: " + e.getMessage());
            }
        }
        Log.i(TAG, "exp applied: comp=" + params.getExposureCompensation()
                + " aeLock=" + params.getAutoExposureLock()
                + " antibanding=" + params.getAntibanding()
                + " scene=" + params.getSceneMode());
    }

    /** Ожидание JPEG-колбэка (именованное вложение). */
    private static final class CamPictureCallback implements Camera.PictureCallback {
        private final CameraService service;
        private volatile byte[] jpeg;

        CamPictureCallback(CameraService service) {
            this.service = service;
        }

        byte[] getJpeg() {
            return jpeg;
        }

        @Override
        public void onPictureTaken(byte[] data, Camera camera) {
            this.jpeg = data;
        }
    }

    /** Preview-колбэк: счётчик кадров для AE-прогрева + последний кадр (Y-яркость)
     *  для fallback-снапшота (без анонимных классов). */
    private static final class CamPreviewWarmup implements Camera.PreviewCallback {
        private volatile int frames;
        private volatile byte[] lastData;
        private volatile Camera.Size lastSize;
        private volatile int lastLuma = -1;

        int getFrames() {
            return frames;
        }

        byte[] getLastData() {
            return lastData;
        }

        Camera.Size getLastSize() {
            return lastSize;
        }

        int getLastLuma() {
            return lastLuma;
        }

        @Override
        public void onPreviewFrame(byte[] data, Camera camera) {
            frames++;
            if (data == null || data.length == 0) {
                return;
            }
            if (lastSize == null) {
                try {
                    lastSize = camera.getParameters().getPreviewSize();
                } catch (Exception e) {
                    return;
                }
            }
            Camera.Size sz = lastSize;
            if (sz == null || data.length < sz.width * sz.height) {
                return;
            }
            int n = sz.width * sz.height;
            long sum = 0;
            int count = 0;
            for (int i = 0; i < n; i += 4) {
                sum += data[i] & 0xFF;
                count++;
            }
            lastData = data;
            lastLuma = (int) (sum / Math.max(1, count));
        }
    }

    /** Средняя яркость JPEG (выборка пикселей); -1 = не удалось декодировать. */
    private static int jpegLuma(byte[] jpeg) {
        if (jpeg == null || jpeg.length == 0) {
            return -1;
        }
        try {
            Bitmap bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
            if (bmp == null) {
                return -1;
            }
            long sum = 0;
            int count = 0;
            for (int y = 0; y < bmp.getHeight(); y += 8) {
                for (int x = 0; x < bmp.getWidth(); x += 8) {
                    int c = bmp.getPixel(x, y);
                    int r = (c >> 16) & 0xFF;
                    int g = (c >> 8) & 0xFF;
                    int b = c & 0xFF;
                    sum += (r + g + b) / 3;
                    count++;
                }
            }
            bmp.recycle();
            return (int) (sum / Math.max(1, count));
        } catch (Throwable t) {
            return -1;
        }
    }

    /** Fallback: JPEG из последнего preview-кадра (NV21) с поворотом по CameraInfo
     *  и зеркалом для фронтальной; null = кадра нет или конвертация не удалась. */
    private static byte[] previewFallbackJpeg(CamPreviewWarmup warmup, boolean front, int orientation) {
        byte[] yuv = warmup == null ? null : warmup.getLastData();
        Camera.Size sz = warmup == null ? null : warmup.getLastSize();
        if (yuv == null || sz == null || sz.width <= 0 || sz.height <= 0) {
            return null;
        }
        try {
            YuvImage yuvImage = new YuvImage(yuv, ImageFormat.NV21, sz.width, sz.height, null);
            ByteArrayOutputStream raw = new ByteArrayOutputStream();
            yuvImage.compressToJpeg(new Rect(0, 0, sz.width, sz.height), 70, raw);
            Bitmap src = BitmapFactory.decodeByteArray(raw.toByteArray(), 0, raw.size());
            if (src == null) {
                return null;
            }
            Matrix m = new Matrix();
            m.postRotate(orientation);
            if (front) {
                m.postScale(-1, 1);
            }
            Bitmap rot = Bitmap.createBitmap(src, 0, 0, src.getWidth(), src.getHeight(), m, true);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            rot.compress(Bitmap.CompressFormat.JPEG, 70, out);
            return out.toByteArray();
        } catch (Throwable t) {
            Log.w(TAG, "preview fallback failed: " + t.getMessage());
            return null;
        }
    }

    /** Хелпер ожидания кадра (без анонимных классов). */
    private static final class CameraSnapshotWaiter {
        private static final int WARMUP_FRAMES = 3;
        private static final long WARMUP_MAX_MS = 1500;

        static void waitAndTake(Camera camera, CamPictureCallback cb, Object lock,
                                CamPreviewWarmup warmup) throws Exception {
            long t0 = System.currentTimeMillis();
            // Фаза AE: первые кадры preview до сошедшейся экспозиции чёрные.
            while (warmup.getFrames() < WARMUP_FRAMES
                    && System.currentTimeMillis() - t0 < WARMUP_MAX_MS) {
                Thread.sleep(100);
            }
            Log.i(TAG, "snapshot warmup frames=" + warmup.getFrames()
                    + " ms=" + (System.currentTimeMillis() - t0));
            synchronized (lock) {
                camera.takePicture(null, null, null, cb);
                long deadline = System.currentTimeMillis() + 6000;
                while (cb.getJpeg() == null && System.currentTimeMillis() < deadline) {
                    lock.wait(500);
                }
            }
            if (cb.getJpeg() == null) {
                throw new IllegalStateException("takePicture timeout");
            }
        }
    }

    // ===== Управление жизненным циклом =====

    /** Сохранение конфига планшета (POST /api/settings): свежий бюджет publish-попыток.
     *  По D4 вечных петель нет; это единственная точка повторной попытки без перезапуска. */
    public void noteConfigChanged() {
        try {
            camHandler.post(new CamConfigChangedTask(this));
        } catch (Exception ignored) {
        }
    }

    public void shutdown() {
        try {
            camHandler.post(new CamShutdownTask(this));
        } catch (Exception ignored) {
        }
    }

    // ===== Именованные задачи для camHandler (анонимные классы запрещены - гоча d8 NPE) =====

    /** Подключить сигналинг. */
    private static final class CamConnectTask implements Runnable {
        private final CameraService service;

        CamConnectTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.connectSignaling();
        }
    }

    /** Сбросить счётчик реконнектов и подключить сигналинг (после enable). */
    private static final class CamEnableTask implements Runnable {
        private final CameraService service;

        CamEnableTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.reconnectAttempt.set(0);
            service.connectSignaling();
        }
    }

    /** Конфиг изменён: свежий бюджет publish-попыток и пересоединение (D4). */
    private static final class CamConfigChangedTask implements Runnable {
        private final CameraService service;

        CamConfigChangedTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.reconnectAttempt.set(0);
            service.clearLastErrorIf("publish endpoint unreachable");
            service.connectSignaling();
        }
    }

    /** Выключение: закрыть сигналинг, сессии, захват. */
    private static final class CamDisableTask implements Runnable {
        private final CameraService service;

        CamDisableTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            if (service.signaling != null) {
                service.signaling.disconnect("disabled");
            }
            service.closeAllSessions("disabled");
            service.stopCapture();
            service.setState(STATE_IDLE, "");
        }
    }

    /** Блокирующий connect() в отдельном потоке. */
    private static final class CamConnectRunner implements Runnable {
        private final CameraService service;

        CamConnectRunner(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            CamSignalingClient client = service.signaling;
            if (client != null) {
                client.connect();
            }
        }
    }

    /** Потеря сигналинга: снести сессии/захват, спланировать реконнект. */
    private static final class CamSignalingLostTask implements Runnable {
        private final CameraService service;

        CamSignalingLostTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.closeAllSessions("signaling closed");
            service.stopCapture();
            service.setStateIdleIfNoSession();
            service.scheduleReconnect();
        }
    }

    /** Регистрация pull-сессии и handleOffer на camThread (аналог offer-ветки dispatchSignaling). */
    private static final class CamPullOfferTask implements Runnable {
        private final CameraService service;
        private final String viewerId;
        private final JSONObject offerPayload;
        private final CamPeerSession session;

        CamPullOfferTask(CameraService service, String viewerId,
                         JSONObject offerPayload, CamPeerSession session) {
            this.service = service;
            this.viewerId = viewerId;
            this.offerPayload = offerPayload;
            this.session = session;
        }

        @Override
        public void run() {
            try {
                service.ensureCapture();
            } catch (Throwable t) {
                service.setLastError("capture start failed: " + t.getMessage());
                session.failPull("capture start failed: " + t.getMessage());
                return;
            }
            synchronized (service.sessionsLock) {
                CamPeerSession prev = service.sessions.get(viewerId);
                if (prev != null) {
                    prev.close("replaced by new offer");
                }
                service.sessions.put(viewerId, session);
            }
            service.setState(STATE_STREAMING, "");
            session.handleOffer(offerPayload);
        }
    }

    /** Диспетчеризация сообщения сигналинга на camThread. */
    private static final class CamDispatchTask implements Runnable {        private final CameraService service;
        private final String type;
        private final JSONObject msg;

        CamDispatchTask(CameraService service, String type, JSONObject msg) {
            this.service = service;
            this.type = type;
            this.msg = msg;
        }

        @Override
        public void run() {
            try {
                service.dispatchSignaling(type, msg);
            } catch (Throwable t) {
                // сбой диспетчеризации не должен убить цикл camThread:
                // фиксируем причину в last_error и продолжаем.
                service.setLastError("dispatch " + type + " failed: " + t.getMessage());
                Log.w(TAG, "dispatch failed: type=" + type, t);
            }
        }
    }

    /** Снятие сессии зрителя на camThread. */
    private static final class CamDropSessionTask implements Runnable {
        private final CameraService service;
        private final String viewerId;
        private final String reason;

        CamDropSessionTask(CameraService service, String viewerId, String reason) {
            this.service = service;
            this.viewerId = viewerId;
            this.reason = reason;
        }

        @Override
        public void run() {
            service.dropSessionOnThread(viewerId, reason);
        }
    }

    /** Полное завершение камерной подсистемы. */
    private static final class CamShutdownTask implements Runnable {
        private final CameraService service;

        CamShutdownTask(CameraService service) {
            this.service = service;
        }

        @Override
        public void run() {
            if (service.signaling != null) {
                service.signaling.disconnect("shutdown");
            }
            service.closeAllSessions("shutdown");
            service.stopCapture();
        }
    }
}
