#!/bin/bash
# Watchdog adb-over-WiFi для парка киосков (запускается таймером systemd --user на хосте)
# Для каждого WiFi-планшета: подключить; если offline — перезапустить adb-сервер хоста.
# Ключгард с хоста не трогаем: на планшетах локальный сторож (.42: service.d/kg_watch.sh).
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
    if [ "$st" != "device" ]; then
        echo "$(date -Is) $t: недоступен (state=$st)"
    fi
done
