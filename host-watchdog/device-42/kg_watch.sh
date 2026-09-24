#!/system/bin/sh
# Бессрочный сторож ключгарда (.42): проверка раз в минуту, лог в kg_watch.log
LOG=/data/local/tmp/kg_watch.log
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 200000 ] && : > "$LOG"
while true; do
  KG=$(dumpsys window policy 2>/dev/null | grep -c 'isStatusBarKeyguard=true')
  if [ "${KG:-0}" -gt 0 ]; then
    echo "=== $(date) keyguard detected" >> "$LOG"
    input keyevent 224 2>>"$LOG"
    input keyevent 82 2>>"$LOG"
    echo "ke82 rc=$?" >> "$LOG"
    wm dismiss-keyguard >> "$LOG" 2>&1
    echo "wm rc=$?" >> "$LOG"
    sleep 3
    if dumpsys window policy 2>/dev/null | grep -q 'isStatusBarKeyguard=true'; then
      input swipe 640 700 640 150 2>>"$LOG"
      echo "swipe tried; after: $(dumpsys window policy 2>/dev/null | grep -o 'isStatusBarKeyguard=[a-z]*' | head -1)" >> "$LOG"
    else
      echo "dismissed ok" >> "$LOG"
    fi
  fi
  sleep 60
done
