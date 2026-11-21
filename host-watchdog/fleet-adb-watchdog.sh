#!/usr/bin/env bash
# Сторож adb-over-WiFi флота информеров 192.168.4.x (systemd --user timer на asrock).
# Залипание adbd (TCP открыт, протокол мёртв) рецидивное на всех четырёх планшетах -
# лечение только перезапуском adbd под su приложения: сессия su живёт в процессе
# приложения и переживает разрыв adb-соединения (прогоны su по adb-сессии убивают
# adbd насовсем: stop рвёт сессию до start - доказано 21.11 на Digma/AT200/Oysters).
# Oysters T74HMi: с вечера 21.11 MAC пиннится ПАТЧЕМ ЯДРА (владелец) - ребут-тест
# 19:11:19 доказал 00:08:22:b0:0d:fc до == после; раньше ROM рандомизировал MAC/IP
# при каждой загрузке (NVRAM WARNING: Err = 0x10). Фолбэк сохранён: если .41 не
# отвечает по /health, ищем планшет по health-дискриминации (информер стоит только
# на 4 планшетах парка; ARP/OUI из asrock не виден - планшеты за роутером, L2 чужой).
# Лог: ~/runs/weather-informer-boot-adb/watchdog.log
# Остановка: systemctl --user disable --now fleet-adb-watchdog.timer
TABLETS="192.168.4.13 192.168.4.21 192.168.4.38 192.168.4.41"
LOG="$HOME/runs/weather-informer-boot-adb/watchdog.log"
mkdir -p "$(dirname "$LOG")"

find_oysters() {
  local ip i
  for ip in 192.168.4.3 192.168.4.5 192.168.4.7 192.168.4.2 192.168.4.14; do
    if curl -s -m 2 "http://$ip:8080/health" 2>/dev/null | grep -q '"status":"ok"'; then
      case " $TABLETS " in *" $ip "*) ;; *) echo "$ip"; return 0 ;; esac
    fi
  done
  for i in $(seq 2 60); do ping -c1 -W1 "192.168.4.$i" >/dev/null 2>&1 & done
  wait
  for i in $(seq 2 60); do
    ip="192.168.4.$i"
    case " $TABLETS " in *" $ip "*) continue ;; esac
    if ping -c1 -W1 "$ip" >/dev/null 2>&1 && curl -s -m 2 "http://$ip:8080/health" 2>/dev/null | grep -q '"status":"ok"'; then
      echo "$ip"; return 0
    fi
  done
}

# Фолбэк только при мёртвом .41: здоровый список не трогаем.
if ! curl -s -m 2 "http://192.168.4.41:8080/health" 2>/dev/null | grep -q '"status":"ok"'; then
  alt=$(find_oysters)
  if [ -n "$alt" ]; then
    echo "$(date -Is) oysters: .41 мёртв, найден на $alt (MAC-патч ядром больше не держит?)" >> "$LOG"
    TABLETS="$TABLETS $alt"
  else
    echo "$(date -Is) oysters: .41 мёртв, в сети не найден - нужен boot/физический заход" >> "$LOG"
  fi
fi

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
