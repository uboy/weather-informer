package ru.weather.informer;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

public class MainActivity extends Activity implements Runnable {
    private static final String TAG = "MainActivity";
    public static final String ACTION_RELOAD = "ru.weather.informer.RELOAD";
    public static final String ACTION_CAM = "ru.weather.informer.CAM";
    private static final long CAM_CONFIRM_TIMEOUT_MS = 20000L;

    private WebView webView;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private long lastReloadMs = 0;
    private FrameLayout camOverlay = null;

    /** Колбэк результата подтверждения на экране планшета (именованное вложение). */
    public interface CamConfirmCallback {
        void onConfirmed(boolean allowed);
    }

    private static class ReloadReceiver extends BroadcastReceiver {
        private final MainActivity activity;

        public ReloadReceiver(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            Log.i(TAG, "Reload broadcast received, reloading informer...");
            if (activity != null) {
                activity.reloadInformer();
            }
        }
    }

    /** Приёмник ACTION_CAM: пересинхронизировать камерную подсистему (отладка). */
    private static class CamReceiver extends BroadcastReceiver {
        private final MainActivity activity;

        public CamReceiver(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            Log.i(TAG, "Cam broadcast received, resync camera subsystem");
            CameraService service = CameraService.getInstance();
            if (activity != null && service != null) {
                activity.runOnUiThread(new CamResyncTask(activity));
            }
        }
    }

    /** Задача пересинхронизации камерного тумблера (именованное вложение). */
    private static class CamResyncTask implements Runnable {
        private final MainActivity activity;

        CamResyncTask(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            activity.refreshOverlayForState();
        }
    }

    /** Клик по кнопкам оверлея подтверждения (именованное вложение). */
    private static class CamConfirmClickListener implements View.OnClickListener {
        private final MainActivity activity;
        private final boolean allowed;

        CamConfirmClickListener(MainActivity activity, boolean allowed) {
            this.activity = activity;
            this.allowed = allowed;
        }

        @Override
        public void onClick(View v) {
            activity.finishCamConfirm(allowed);
        }
    }

    /** Таймаут оверлея подтверждения (именованное вложение). */
    private static class CamConfirmTimeout implements Runnable {
        private final MainActivity activity;

        CamConfirmTimeout(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            activity.finishCamConfirm(false);
        }
    }

    private final ReloadReceiver reloadReceiver = new ReloadReceiver(this);
    private final CamReceiver camReceiver = new CamReceiver(this);

    @Override
    public void run() {
        if (isFinishing() || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && isDestroyed())) {
            return;
        }
        if (webView != null) {
            webView.loadUrl("http://127.0.0.1:8080/?force=1&t=" + System.currentTimeMillis());
        }
    }

    public void scheduleReload(long delayMs) {
        mainHandler.postDelayed(this, delayMs);
    }

    public void reloadInformer() {
        long now = SystemClock.elapsedRealtime();
        if (now - lastReloadMs < 1000) {
            Log.d(TAG, "reloadInformer ignored (debounced within 1000ms)");
            return;
        }
        lastReloadMs = now;
        runOnUiThread(this);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && intent.getBooleanExtra("force_reload", false)) {
            Log.i(TAG, "onNewIntent with force_reload, reloading informer...");
            reloadInformer();
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 1. Экран всегда включён
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // 2. Скрытие системных панелей (Immersive mode)
        hideSystemUI();

        // 3. Запуск фонового веб-сервера
        Intent serviceIntent = new Intent(this, WebServerService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }

        // 3b. Инициализация камерной подсистемы (фаза 1; enabled по умолчанию false)
        CameraService.init(getApplicationContext());

        // 4. Регистрация приемников broadcast
        try {
            registerReceiver(reloadReceiver, new IntentFilter(ACTION_RELOAD));
        } catch (Exception e) {
            Log.e(TAG, "Failed to register reloadReceiver: " + e.getMessage());
        }
        try {
            registerReceiver(camReceiver, new IntentFilter(ACTION_CAM));
        } catch (Exception e) {
            Log.e(TAG, "Failed to register camReceiver: " + e.getMessage());
        }

        // 5. Настройка WebView Kiosk
        webView = new WebView(this);
        setContentView(webView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowContentAccess(true);
        ws.setUseWideViewPort(true);
        ws.setLoadWithOverviewMode(true);
        ws.setCacheMode(WebSettings.LOAD_NO_CACHE);

        webView.setWebViewClient(new KioskClient(this));

        if (InformerServer.getInstance() != null) {
            InformerServer.getInstance().setActivity(this);
        }

        mainHandler.postDelayed(this, 500);
        // Гонка после install -r: сервис создаёт InformerServer позже Activity;
        // ре-аттач активности, иначе cam-оверлей не найдёт foreground activity.
        mainHandler.postDelayed(new CamAttachTask(this), 1500);
        mainHandler.postDelayed(new CamAttachTask(this), 4000);
    }

    /** Отложенный ре-аттач activity в InformerServer (именованное статическое вложение). */
    private static final class CamAttachTask implements Runnable {
        private final MainActivity activity;

        CamAttachTask(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            if (activity.isFinishing() || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && activity.isDestroyed())) {
                return;
            }
            InformerServer server = InformerServer.getInstance();
            if (server != null && server.getActivity() != activity) {
                server.setActivity(activity);
                Log.i(TAG, "activity re-attached to InformerServer (delayed)");
            }
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemUI();
        if (InformerServer.getInstance() != null) {
            InformerServer.getInstance().setActivity(this);
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        mainHandler.removeCallbacksAndMessages(null);
        try {
            unregisterReceiver(reloadReceiver);
        } catch (Exception ignored) {}
        try {
            unregisterReceiver(camReceiver);
        } catch (Exception ignored) {}
        if (InformerServer.getInstance() != null && InformerServer.getInstance().getActivity() == this) {
            InformerServer.getInstance().setActivity(null);
        }
        CameraService service = CameraService.getInstance();
        if (service != null) {
            service.shutdown();
        }
        if (webView != null) {
            webView.stopLoading();
            webView.setWebViewClient(null);
            webView.destroy();
            webView = null;
        }
    }

    // ===== Оверлей подтверждения камеры (privacy 5.1.6) =====

    /**
     * Показать оверлей подтверждения на экране планшета. Вызывается с любого потока.
     * Камера меняет флаг ТОЛЬКО после тапа "Разрешить" на экране.
     */
    public void showCamConfirmOverlay(final boolean enable, final CamConfirmCallback callback) {
        mainHandler.post(new CamShowConfirmTask(this, enable, callback));
    }

    /** Задача показа оверлея на UI-потоке (именованное СТАТИЧЕСКОЕ вложение: не-статические inner-классы валят d8 NPE). */
    private static final class CamShowConfirmTask implements Runnable {
        private final MainActivity activity;
        private final boolean enable;
        private final CamConfirmCallback callback;

        CamShowConfirmTask(MainActivity activity, boolean enable, CamConfirmCallback callback) {
            this.activity = activity;
            this.enable = enable;
            this.callback = callback;
        }

        @Override
        public void run() {
            activity.showCamConfirmOverlayOnUi(enable, callback);
        }
    }

    private void showCamConfirmOverlayOnUi(boolean enable, CamConfirmCallback callback) {
        if (isFinishing() || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && isDestroyed())) {
            if (callback != null) {
                callback.onConfirmed(false);
            }
            return;
        }
        hideCamConfirmOverlay();
        FrameLayout root = (FrameLayout) findViewById(android.R.id.content);
        if (root == null) {
            if (callback != null) {
                callback.onConfirmed(false);
            }
            return;
        }
        FrameLayout overlay = new FrameLayout(this);
        overlay.setBackgroundColor(0xEE101820);
        overlay.setClickable(true);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        FrameLayout.LayoutParams boxParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER);
        boxParams.setMargins(48, 48, 48, 48);
        box.setLayoutParams(boxParams);

        TextView title = new TextView(this);
        title.setText(enable ? "Включить камеру?" : "Выключить камеру?");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setGravity(Gravity.CENTER);
        box.addView(title);

        TextView hint = new TextView(this);
        hint.setText("Подтвердите тапом на экране планшета.\nИзменение из сети невозможно.");
        hint.setTextColor(0xFFB0BEC5);
        hint.setTextSize(14);
        hint.setGravity(Gravity.CENTER);
        box.addView(hint);

        Button allow = new Button(this);
        allow.setText(enable ? "Разрешить" : "Выключить");
        allow.setOnClickListener(new CamConfirmClickListener(this, true));
        box.addView(allow);

        Button deny = new Button(this);
        deny.setText("Отмена");
        deny.setOnClickListener(new CamConfirmClickListener(this, false));
        box.addView(deny);

        overlay.addView(box);
        root.addView(overlay, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        camOverlay = overlay;

        mainHandler.postDelayed(new CamConfirmTimeout(this), CAM_CONFIRM_TIMEOUT_MS);
        Log.i(TAG, "cam confirm overlay shown: enable=" + enable);
    }

    /** Завершить подтверждение (тап или таймаут). Только с UI-потока. */
    public void finishCamConfirm(boolean allowed) {
        hideCamConfirmOverlay();
        CameraService service = CameraService.getInstance();
        if (service != null && allowed) {
            service.setEnabled(!service.isEnabled());
        }
        Log.i(TAG, "cam confirm finished: allowed=" + allowed);
    }

    private void hideCamConfirmOverlay() {
        if (camOverlay != null) {
            FrameLayout root = (FrameLayout) findViewById(android.R.id.content);
            if (root != null) {
                root.removeView(camOverlay);
            }
            camOverlay = null;
        }
    }

    /** Пересинхронизация: убрать оверлей, если состояние уже совпадает (ACTION_CAM). */
    public void refreshOverlayForState() {
        hideCamConfirmOverlay();
    }

    private void hideSystemUI() {
        View decorView = getWindow().getDecorView();
        decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN);
    }
}
