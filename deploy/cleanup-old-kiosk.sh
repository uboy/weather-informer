#!/bin/bash
# Снос старого WebView Kiosk, перевод парка на приложение WeatherInformer (ru.weather.informer).
# Наше приложение само LAUNCHER+HOME (BootReceiver поднимает его после ребута).
# Использование: deploy/cleanup-old-kiosk.sh <serial|ip:5555> [...]
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -ge 1 ] || { echo "usage: $0 <serial|ip:5555> [...]"; exit 1; }
OLD_PKG="uk.nktnet.webviewkiosk"
FAILS=0
for S in "$@"; do
  echo "=== $S ==="
  if ! adb -s "$S" get-state >/dev/null 2>&1; then echo "$S: adb недоступен"; FAILS=$((FAILS+1)); continue; fi
  # 1. Свежий APK информера (поля интервалов в :8080/settings)
  adb -s "$S" install -r "$DIR/apks/informer.apk" >/dev/null && echo "  apk: установлен" || { echo "  apk: FAIL"; FAILS=$((FAILS+1)); }
  # 2. Снести старый kiosk
  if adb -s "$S" shell pm list packages 2>/dev/null | tr -d "\r" | grep -q "$OLD_PKG"; then
    if adb -s "$S" uninstall "$OLD_PKG" >/dev/null 2>&1; then echo "  старый kiosk: удалён"; else echo "  старый kiosk: НЕ удалился (проверить вручную)"; FAILS=$((FAILS+1)); fi
  else
    echo "  старый kiosk: не установлен"
  fi
  # 3. Файлы старой схемы (kiosk читал их с /sdcard/Download; наше приложение живёт в filesDir/web)
  adb -s "$S" shell "rm -f /sdcard/Download/informer.html /sdcard/Download/jquery.min.js" 2>/dev/null
  echo "  /sdcard/Download: старые informer.html/jquery удалены (config.json не трогали)"
  # 4. Поднять информер
  adb -s "$S" shell am start -n ru.weather.informer/.MainActivity >/dev/null 2>&1 && echo "  информер: запущен" || echo "  информер: am start не сработал"
  # 5. Факт
  adb -s "$S" shell pm list packages 2>/dev/null | tr -d "\r" | grep -E "ru\.weather\.informer|webviewkiosk" | sed "s/^/  pkg: /"
done
[ "$FAILS" -eq 0 ] || { echo "проблемы на $FAILS планшетах"; exit 1; }
echo "Готово. После удаления старого kiosk система при нажатии Home попросит выбрать лаунчер — выбрать «Информер» → «Всегда»."
