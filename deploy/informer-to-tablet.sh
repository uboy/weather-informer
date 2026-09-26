#!/bin/bash
# Деплой информера на один планшет: deploy/informer-to-tablet.sh <adb-serial>
set -e
S="${1:?использование: $0 <adb-serial|ip:5555>}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Деплой на $S ==="

# Проверяем доступность устройства
adb -s "$S" get-state >/dev/null

# Пушим файлы контента
adb -s "$S" push "$DIR/informer.html" /sdcard/Download/informer.html
adb -s "$S" push "$DIR/jquery.min.js" /sdcard/Download/jquery.min.js

# Выдаем права на чтение хранилища для Android 6.0+
adb -s "$S" shell "pm grant uk.nktnet.webviewkiosk android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true"

# Пробуем перевести adbd в root, если поддерживается
adb -s "$S" root 2>/dev/null || true

# Заливаем скрипт настройки
adb -s "$S" push "$DIR/deploy/setup-kiosk.sh" /data/local/tmp/setup-kiosk.sh >/dev/null
adb -s "$S" shell "chmod 755 /data/local/tmp/setup-kiosk.sh"

# Выполняем скрипт под root с явным контролем ошибок
EXEC_CMD="sh -c 'if [ \$(id -u) -eq 0 ]; then sh /data/local/tmp/setup-kiosk.sh; elif which su >/dev/null 2>&1; then su -c \"sh /data/local/tmp/setup-kiosk.sh\" || su 0 \"sh /data/local/tmp/setup-kiosk.sh\"; else exit 1; fi'"

if ! adb -s "$S" shell "$EXEC_CMD"; then
    echo "ERROR [$S]: Не удалось выполнить настройку киоска с правами root!" >&2
    adb -s "$S" shell "rm -f /data/local/tmp/setup-kiosk.sh" 2>/dev/null || true
    exit 1
fi

# Убираем временный файл
adb -s "$S" shell "rm -f /data/local/tmp/setup-kiosk.sh" 2>/dev/null || true

echo "$S: informer.html + jquery.min.js обновлены, киоск успешно настроен и запущен"
