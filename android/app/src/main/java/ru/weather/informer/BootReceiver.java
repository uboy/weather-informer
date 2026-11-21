package ru.weather.informer;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import java.io.BufferedReader;
import java.io.InputStreamReader;

public class BootReceiver extends BroadcastReceiver implements Runnable {
    private static final String TAG = "BootReceiver";
    // Единый boot-скрипт флота: пин MAC + восстановление adb-over-WiFi + подъём киоска.
    // service.d на этих ROM не исполняется (SuperSU 2.79/2.82 игнорирует, Magisk 28.1
    // пишет "environment incomplete"), поэтому исполняем его сами через su.
    private static final String BOOT_SCRIPT = "/data/adb/service.d/informer_autostart.sh";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction()) ||
            "android.intent.action.QUICKBOOT_POWERON".equals(intent.getAction())) {

            Intent i = new Intent(context, MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(i);

            // Исполняется в отдельном потоке: в onReceive блокировать нельзя.
            new Thread(this, "boot-script").start();
        }
    }

    /** Исполняет единый boot-скрипт через su; результат - в logcat и в лог самого скрипта. */
    @Override
    public void run() {
        try {
            Process p = Runtime.getRuntime().exec(
                    new String[]{"su", "-c", "sh " + BOOT_SCRIPT + " boot-hook"});
            BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
            StringBuilder tail = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null && tail.length() < 1000) {
                tail.append(line).append('\n');
            }
            int code = p.waitFor();
            Log.i(TAG, "boot script exit=" + code + " tail=" + tail);
        } catch (Exception e) {
            Log.e(TAG, "boot script failed: " + e);
        }
    }
}
