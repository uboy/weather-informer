#!/usr/bin/env bash
# Сторож adb-over-WiFi флота информеров 192.168.4.x (systemd --user timer на asrock).
# Залипание adbd (TCP открыт, протокол мёртв) рецидивное на всех четырёх планшетах -
# лечение только перезапуском adbd под su приложения: сессия su живёт в процессе
# приложения и переживает разрыв adb-соединения (прогоны su по adb-сессии убивают
# adbd насовсем: stop рвёт сессию до start - доказано 21.11 на Digma/AT200/Oysters).
# Лог: ~/runs/weather-informer-boot-adb/watchdog.log
# Остановка: systemctl --user disable --now fleet-adb-watchdog.timer
TABLETS="192.168.4.13 192.168.4.21 192.168.4.38 192.168.4.41"
LOG="$HOME/runs/weather-informer-boot-adb/watchdog.log"
mkdir -p "$(dirname "$LOG")"

for ip in $TABLETS; do
  t="$ip:5555"
  adb connect "$t" >/dev/null 2>&1
  sleep 1
  st=$(adb -s "$t" get-state 2>/dev/null)
  if [ "$st" != "device" ]; then
    adb disconnect "$t" >/dev/null 2>&1
    sleep 1
    adb connect "$t" >/dev/null 2>&1
    sleep 2
    st=$(adb -s "$t" get-state 2>/dev/null)
  fi
  [ "$st" = "device" ] && continue

  # HTTP встроенного сервера жив? (он не зависит от adbd)
  if curl -s --max-time 8 "http://$ip:8080/health" | grep -q '"status":"ok"'; then
    resp=$(curl -s --max-time 25 -X POST -d '{"mode":"wake"}' "http://$ip:8080/api/adb/restart")
    sleep 4
    adb disconnect "$t" >/dev/null 2>&1
    adb connect "$t" >/dev/null 2>&1
    sleep 2
    st2=$(adb -s "$t" get-state 2>/dev/null)
    echo "$(date -Is) $t state=$st wake=$resp -> $st2" >> "$LOG"
  else
    echo "$(date -Is) $t state=$st app-down (HTTP 8080 мёртв - нужен boot/физический заход)" >> "$LOG"
  fi
done
