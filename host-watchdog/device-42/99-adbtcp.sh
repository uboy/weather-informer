#!/system/bin/sh
# adb-over-WiFi watchdog + автосброс ключгарда (ROM Digma игнорирует lockscreen.disabled)
setprop persist.adb.tcp.port 5555
setprop service.adb.tcp.port 5555
stop adbd
start adbd

# Быстрый разгон после загрузки: будим и свайпаем каждые 30с первые 15 минут
(
  sleep 30
  i=0
  while [ $i -lt 30 ]; do
    input keyevent 224
    input swipe 512 400 512 100
    i=$((i+1))
    sleep 30
  done
) &

# Бессрочный сторож ключгарда (условный, раз в минуту)

(
  sleep 90
  DAYMARK=""
  while true; do
    NEED=0
    [ "$(getprop service.adb.tcp.port)" != "5555" ] && NEED=1
    netstat -tln 2>/dev/null | grep -q ":5555 " || NEED=1
    TODAY=$(date +%F)
    [ "$TODAY" != "$DAYMARK" ] && [ "$(date +%H)" = "04" ] && { DAYMARK=$TODAY; NEED=1; }
    if [ "$NEED" = "1" ]; then
      setprop persist.adb.tcp.port 5555
      setprop service.adb.tcp.port 5555
      stop adbd 2>/dev/null
      start adbd
    fi
    sleep 300
  done
) &
