package ru.weather.informer;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.FrameLayout;

public class MainActivity extends Activity implements Runnable {
    private WebView webView;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    public void run() {
        if (webView != null) {
            webView.clearCache(true);
            webView.loadUrl("http://127.0.0.1:8080/");
        }
    }

    public void scheduleReload(long delayMs) {
        mainHandler.postDelayed(this, delayMs);
    }

    public void reloadInformer() {
        runOnUiThread(this);
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

        // 4. Настройка WebView Kiosk
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
