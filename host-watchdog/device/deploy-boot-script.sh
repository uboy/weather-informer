#!/usr/bin/env bash
# Деплой единого boot-скрипта на планшет флота информеров.
# Использование: deploy-boot-script.sh <ip> <mac>
# Пример:         deploy-boot-script.sh 192.168.4.5 00:08:22:b0:0d:fc
# Кладёт /data/adb/service.d/informer_autostart.sh с пином MAC данного устройства
# (шаблон рядом с этим скриптом), затем делает контрольный прогон вручную.
set -euo pipefail

IP="${1:?usage: deploy-boot-script.sh <ip> <mac>}"
MAC="${2:?usage: deploy-boot-script.sh <ip> <mac>}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$DIR/informer_autostart.sh.template"

[ -f "$TEMPLATE" ] || { echo "template not found: $TEMPLATE"; exit 1; }

TMP="/data/local/tmp/informer_autostart.new.sh"
LOCAL_TMP="$(mktemp)"
sed "s/@MAC@/$MAC/" "$TEMPLATE" > "$LOCAL_TMP"

echo "=== push to $IP ($MAC) ==="
adb connect "$IP:5555" || true
sleep 2
adb -s "$IP:5555" push "$LOCAL_TMP" "$TMP"
# Опасность: длинные цепочки внутри su -c ломаются о тройную вложенность
# (ssh -> adb shell -> su); выполняем по одной команде на su (21.11, AT200).
adb -s "$IP:5555" shell su -c "mkdir -p /data/adb/service.d"
adb -s "$IP:5555" shell su -c "rm -f /data/adb/service.d/informer_autostart.sh"
adb -s "$IP:5555" shell su -c "cp $TMP /data/adb/service.d/informer_autostart.sh"
adb -s "$IP:5555" shell su -c "chmod 750 /data/adb/service.d/informer_autostart.sh"
adb -s "$IP:5555" shell su -c "chown 0:0 /data/adb/service.d/informer_autostart.sh"
adb -s "$IP:5555" shell su -c "restorecon /data/adb/service.d/informer_autostart.sh" 2>/dev/null || true
adb -s "$IP:5555" shell su -c "rm -f $TMP"
adb -s "$IP:5555" shell su -c "ls -la /data/adb/service.d/"
rm -f "$LOCAL_TMP"
# Лог скрипта: create в shell_data_file запрещён confined su-доменам - предсоздаём под shell uid.
adb -s "$IP:5555" shell "touch /data/local/tmp/informer_boot.log; chmod 666 /data/local/tmp/informer_boot.log"
echo "=== контрольный прогон (manual: MAC-пин+киоск, БЕЗ adbd-блока) ==="
echo "=== adbd-блок по adb-сессии не гонять: stop adbd убивает сессию до start (21.11) ==="
echo "=== его проверяют через HTTP wake: curl -X POST -d '{\"mode\":\"wake\"}' http://$IP:8080/api/adb/restart ==="
adb -s "$IP:5555" shell su -c "sh /data/adb/service.d/informer_autostart.sh manual"
adb -s "$IP:5555" shell su -c "cat /data/adb/service.d/boot.log 2>/dev/null || cat /data/local/tmp/informer_boot.log 2>/dev/null" | tail -10
