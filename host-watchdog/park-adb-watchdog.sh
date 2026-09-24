#!/bin/bash
# Watchdog adb-over-WiFi для парка киосков (запускается таймером systemd --user на хосте)
# Для каждого WiFi-планшета: подключить; если offline — перезапустить adb-сервер хоста;
# если устройство за ключгардом — сбросить свайпом (киоск-приложение само держит экран дальше).
TABLETS="192.168.1.20:5555 192.168.1.30:5555 192.168.1.31:5555 192.168.1.42:5555"
for t in $TABLETS; do
    adb connect "$t" > /dev/null 2>&1
    st=$(adb -s "$t" get-state 2>/dev/null)
    if [ "$st" != "device" ]; then
        # мёртвая сессия хоста: перезапуск adb-сервера и повтор
        adb disconnect "$t" > /dev/null 2>&1
        sleep 1
        adb connect "$t" > /dev/null 2>&1
        st=$(adb -s "$t" get-state 2>/dev/null)
    fi
    if [ "$st" = "device" ]; then
        # автосброс ключгарда (после загрузки планшета киоск за ключгардом).
        # Флаги зависят от ROM: isStatusBarKeyguard (Digma/.42), keyguardshown, isKeyguardShowing.
        kg=$(adb -s "$t" shell "dumpsys window policy 2>/dev/null" | grep -icE "isStatusBarKeyguard=true|keyguardshown=true|isKeyguardShowing=true")
        if [ "${kg:-0}" -gt 0 ]; then
            # Digma-ключгард на swipe не реагирует: wake + MENU + dismiss
            adb -s "$t" shell "input keyevent 224; input keyevent 82; wm dismiss-keyguard" > /dev/null 2>&1
            echo "$(date -Is) $t: keyguard dismissed"
        fi
    else
        echo "$(date -Is) $t: недоступен (state=$st)"
    fi
done
