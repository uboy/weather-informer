package ru.weather.informer;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

public class WebServerService extends Service {
    private static final String TAG = "WebServerService";
    private static final String CHANNEL_ID = "informer_server_channel";
    private static final int NOTIF_ID = 1001;

    private InformerServer server;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    @Override
    public void onCreate() {
        super.onCreate();

        // 1. Защита от сна и MediaTek DuraSpeed / Doze
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "WeatherInformer:ServerWakeLock");
                wakeLock.acquire();
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed to acquire WakeLock: " + e.getMessage());
        }

        try {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "WeatherInformer:ServerWifiLock");
                wifiLock.acquire();
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed to acquire WifiLock: " + e.getMessage());
        }

        // 2. Foreground Service Notification
        startForeground(NOTIF_ID, createNotification());

        // 3. Запуск HTTP-сервера
        try {
            server = new InformerServer(this);
            server.start();
            Log.i(TAG, "WebServerService started successfully");
        } catch (Exception e) {
            Log.e(TAG, "Failed to start HTTP server: " + e.getMessage());
        }
    }

    private Notification createNotification() {
        Intent notifIntent = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, notifIntent,
                (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) ? PendingIntent.FLAG_IMMUTABLE : 0);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel chan = new NotificationChannel(
                    CHANNEL_ID,
                    "Weather Informer Server",
                    NotificationManager.IMPORTANCE_LOW
            );
            chan.setDescription("Фоновый веб-сервер информера");
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.createNotificationChannel(chan);
            }

            return new Notification.Builder(this, CHANNEL_ID)
                    .setContentTitle("Погодный Информер")
                    .setContentText("Веб-сервер работает на порту " + InformerServer.PORT)
                    .setSmallIcon(android.R.drawable.ic_menu_compass)
                    .setContentIntent(pi)
                    .setOngoing(true)
                    .build();
        } else {
            // Android 5.0 - 7.1
            return new Notification.Builder(this)
                    .setContentTitle("Погодный Информер")
                    .setContentText("Веб-сервер работает на порту " + InformerServer.PORT)
                    .setSmallIcon(android.R.drawable.ic_menu_compass)
                    .setContentIntent(pi)
                    .setOngoing(true)
                    .build();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (server != null) {
            server.stop();
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        if (wifiLock != null && wifiLock.isHeld()) {
            wifiLock.release();
        }
        Log.i(TAG, "WebServerService destroyed");
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
