#!/bin/bash
# Деплой информера на один планшет: deploy/informer-to-tablet.sh <adb-serial>
#   <adb-serial> — USB-серийник или IP:5555 (adb-over-wifi)
set -e
S="${1:?использование: $0 <adb-serial|ip:5555>}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
adb -s "$S" push "$DIR/informer.html" /sdcard/Download/informer.html
adb -s "$S" push "$DIR/jquery.min.js" /sdcard/Download/jquery.min.js
adb -s "$S" shell "am force-stop uk.nktnet.webviewkiosk; sleep 1; am start -n uk.nktnet.webviewkiosk/.MainActivity" > /dev/null
echo "$S: informer.html + jquery.min.js обновлены, киоск перезапущен"
