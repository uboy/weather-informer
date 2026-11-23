package ru.weather.informer;

import android.util.Log;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

/**
 * Обслуживание adb-over-WiFi на самом планшете (карточка boot-восстановления, задачи 1-2).
 * На этих ROM управлять adbd и tcp-пропами может только root, поэтому всё идёт через su.
 * «Разбудить» (wake) = восстановить persist/service tcp-пропы и перезапустить adbd -
 * лечит и стёртый при загрузке persist, и залипший adbd (TCP открыт, протокол мёртв).
 * «Перезапустить» (restart) = только stop/start adbd.
 * Вызов приходит по HTTP встроенного сервера, поэтому разрыв adb-соединения ему не страшен.
 */
public final class AdbMaintainer {
    private static final String TAG = "AdbMaintainer";
    private static final long SU_TIMEOUT_MS = 10000;
    private static final String ADB_PORT = "5555";

    /**
     * F4 (23.11): определение PID adbd. Наивный «pidof adbd» ненадёжен на флоте:
     * ROM Digma (Android 6) игнорирует аргумент pidof и возвращает PID ВСЕХ процессов
     * (~150 чисел в одной строке), у IRBIS «ps -A» пуст, у AT200 su-вывод может нести
     * мусор superuser-обвязки. Пробуем по цепочке с проверкой «только цифры»:
     * pidof (первое поле) -> ps -A -> legacy ps (колонка 2 = PID). mksh-safe, без awk.
     */
    private static final String ADB_PID_PROBE =
        "n=$(pidof adbd 2>/dev/null); n=${n%% *}; "
        + "case \"$n\" in ''|*[!0-9]*) n=\"\";; esac; "
        + "[ -n \"$n\" ] || { set -- $(ps -A 2>/dev/null | grep -w adbd | grep -v grep); "
        + "[ $# -ge 2 ] && n=$2; }; "
        + "[ -n \"$n\" ] || { set -- $(ps 2>/dev/null | grep -w adbd | grep -v grep); "
        + "[ $# -ge 2 ] && n=$2; }; "
        + "case \"$n\" in ''|*[!0-9]*) n=\"\";; esac; "
        + "echo \"$n\"";

    private AdbMaintainer() {
    }

    /** JSON-статус: persist/runtime порты, жив ли adbd, получен ли root через su. */
    public static String statusJson() {
        String persist = firstLine(su("getprop persist.adb.tcp.port"));
        String runtime = firstLine(su("getprop service.adb.tcp.port"));
        String pid = firstLine(su(ADB_PID_PROBE));
        if (!pid.matches("\\d+")) {
            pid = "";
        }
        String uid = firstLine(su("id -u"));
        boolean root = "0".equals(uid);
        boolean running = !pid.isEmpty();
        Log.i(TAG, "status: persist=" + persist + " runtime=" + runtime
                + " adbdPid=" + pid + " root=" + root);
        return "{\"persistPort\":\"" + esc(persist) + "\",\"runtimePort\":\"" + esc(runtime)
                + "\",\"adbdPid\":\"" + esc(pid) + "\",\"adbdRunning\":" + running
                + ",\"root\":" + root + "}";
    }

    /**
     * Перезапуск adbd. mode "wake" дополнительно восстанавливает tcp-пропы (после загрузки
     * persist стирается и adbd стартует без tcp-слушателя). Ответ - JSON с результатом и
     * самопроверкой (init.svc.adbd + tcp-пропы), чтобы вердикт не требовал отдельного опроса.
     * Паузы вокруг stop/start: без них init может обработать start до завершения stop.
     */
    public static String restartJson(String mode) {
        boolean wake = "wake".equals(mode);
        StringBuilder cmd = new StringBuilder();
        if (wake) {
            cmd.append("setprop persist.adb.tcp.port ").append(ADB_PORT).append("; ")
               .append("setprop service.adb.tcp.port ").append(ADB_PORT).append("; ");
        }
        cmd.append("stop adbd; sleep 1; start adbd; sleep 1")
           .append("; echo svc=$(getprop init.svc.adbd)")
           .append(" persist=$(getprop persist.adb.tcp.port)")
           .append(" runtime=$(getprop service.adb.tcp.port)");
        Log.i(TAG, "restart mode=" + mode + " cmd=" + cmd);
        String res = su(cmd.toString());
        boolean ok = !res.startsWith("ERR") && res.contains("svc=running");
        Log.i(TAG, "restart result: " + res);
        return "{\"mode\":\"" + (wake ? "wake" : "restart") + "\",\"ok\":" + ok
                + ",\"result\":\"" + esc(res) + "\"}";
    }

    /** Исполняет команду под su; возвращает stdout (или stderr, либо ERR:...). */
    private static synchronized String su(String command) {
        Process p = null;
        try {
            p = Runtime.getRuntime().exec(new String[]{"su", "-c", command});
            Gobbler out = new Gobbler(p.getInputStream());
            Gobbler err = new Gobbler(p.getErrorStream());
            out.start();
            err.start();
            long deadline = System.currentTimeMillis() + SU_TIMEOUT_MS;
            boolean finished = false;
            while (System.currentTimeMillis() < deadline) {
                try {
                    p.exitValue();
                    finished = true;
                    break;
                } catch (IllegalThreadStateException stillRunning) {
                    try {
                        Thread.sleep(100);
                    } catch (InterruptedException ie) {
                        break;
                    }
                }
            }
            if (!finished) {
                p.destroy();
                return "ERR:timeout";
            }
            out.join(1000);
            err.join(1000);
            String o = out.text.toString().trim();
            return o.isEmpty() ? err.text.toString().trim() : o;
        } catch (Exception e) {
            Log.e(TAG, "su exec failed: " + e);
            return "ERR:" + e.getClass().getSimpleName();
        } finally {
            if (p != null) {
                p.destroy();
            }
        }
    }

    private static String firstLine(String s) {
        if (s == null || s.isEmpty()) {
            return "";
        }
        int nl = s.indexOf('\n');
        return (nl >= 0 ? s.substring(0, nl) : s).trim();
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    /** Тихий читатель потока процесса с ограничением объёма. */
    private static final class Gobbler extends Thread {
        private final InputStream in;
        private final StringBuilder text = new StringBuilder();

        Gobbler(InputStream in) {
            this.in = in;
            setDaemon(true);
        }

        @Override
        public void run() {
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            try {
                String line;
                while ((line = r.readLine()) != null) {
                    if (text.length() < 4000) {
                        text.append(line).append('\n');
                    }
                }
            } catch (Exception ignored) {
                // поток оборван destroy() - это нормальный путь таймаута
            }
        }
    }
}
