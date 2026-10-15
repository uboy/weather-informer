package ru.weather.informer;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.FrameLayout;

public class MainActivity extends Activity implements Runnable {
    private static final String TAG = "MainActivity";
    public static final String ACTION_RELOAD = "ru.weather.informer.RELOAD";

    private WebView webView;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private long lastReloadMs = 0;

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

    private final ReloadReceiver reloadReceiver = new ReloadReceiver(this);

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

        // 4. Регистрация приемника broadcast на перезагрузку
        try {
            registerReceiver(reloadReceiver, new IntentFilter(ACTION_RELOAD));
        } catch (Exception e) {
            Log.e(TAG, "Failed to register reloadReceiver: " + e.getMessage());
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
        if (InformerServer.getInstance() != null && InformerServer.getInstance().getActivity() == this) {
            InformerServer.getInstance().setActivity(null);
        }
        if (webView != null) {
            webView.stopLoading();
            webView.setWebViewClient(null);
            webView.destroy();
            webView = null;
        }
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
