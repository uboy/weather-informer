package ru.weather.informer;

import android.webkit.WebView;
import android.webkit.WebViewClient;

public class KioskClient extends WebViewClient {
    private final MainActivity activity;

    public KioskClient(MainActivity activity) {
        this.activity = activity;
    }

    @Override
    public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
        activity.scheduleReload(1500);
    }
}
