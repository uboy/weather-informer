package ru.weather.informer;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.SurfaceTexture;
import android.hardware.Camera;
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

import java.net.URI;
import java.util.Iterator;
import java.util.LinkedHashMap;
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

    private static volatile CameraService instance;
    private static final Object INSTANCE_LOCK = new Object();

    private final Context appContext;
    private final HandlerThread camThread;
    private final Handler camHandler;
    private final Handler mainHandler;
    private final Object sessionsLock = new Object();
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
        // Арбитр: держим замок на весь снапшот, конфликт с стримом исключён.
        synchronized (this) {
            if (!STATE_IDLE.equals(state)) {
                return SNAP_BUSY;
            }
            setState(STATE_SNAPSHOT, "");
            try {
                byte[] jpeg = captureJpeg();
                if (jpeg != null) {
                    clearLastErrorIf("snapshot failed");
                }
                jpegOut[0] = jpeg;
                return jpeg != null ? SNAP_OK : SNAP_ERROR;
            } catch (Throwable t) {
                setLastError("snapshot failed: " + t.getMessage());
                return SNAP_ERROR;
            } finally {
                setStateIdleIfNoSession();
            }
        }
    }

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
        Camera camera = Camera.open(cameraId);
        try {
            SurfaceTexture dummy = new SurfaceTexture(0);
            try {
                camera.setPreviewTexture(dummy);
            } catch (Exception e) {
                Log.w(TAG, "setPreviewTexture: " + e.getMessage());
            }
            Camera.Parameters params = camera.getParameters();
            params.setJpegQuality(70);
            try {
                params.setPreviewSize(320, 240);
            } catch (Exception ignored) {
            }
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
            return cb.getJpeg();
        } finally {
            try {
                camera.stopPreview();
            } catch (Exception ignored) {
            }
            camera.release();
            Log.i(TAG, "snapshot camera released");
        }
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

    /** Счётчик preview-кадров для AE-прогрева (без анонимных классов). */
    private static final class CamPreviewWarmup implements Camera.PreviewCallback {
        private volatile int frames;

        int getFrames() {
            return frames;
        }

        @Override
        public void onPreviewFrame(byte[] data, Camera camera) {
            frames++;
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

    /** Диспетчеризация сообщения сигналинга на camThread. */
    private static final class CamDispatchTask implements Runnable {
        private final CameraService service;
        private final String type;
        private final JSONObject msg;

        CamDispatchTask(CameraService service, String type, JSONObject msg) {
            this.service = service;
            this.type = type;
            this.msg = msg;
        }

        @Override
        public void run() {
            service.dispatchSignaling(type, msg);
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
