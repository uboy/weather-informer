#!/bin/sh
# Настройка WebView Kiosk на Android-устройстве.
# Запускается под root (через su или прямой adb root).
set -e

APP_DIR="/data/data/uk.nktnet.webviewkiosk"
if [ ! -d "$APP_DIR" ]; then
    # Киоск не установлен — выходим без ошибки
    exit 0
fi

PREFS_DIR="$APP_DIR/shared_prefs"
mkdir -p "$PREFS_DIR"

# 1. Надежное определение UID:GID приложения без внешних бинарников (toybox/toolbox/busybox agnostic)
get_app_uid() {
    # Метод 1: dumpsys package (самый надежный способ во всех версиях Android)
    UID_DUMP=$(dumpsys package uk.nktnet.webviewkiosk 2>/dev/null | while IFS= read -r line; do
        case "$line" in
            *userId=*|*appId=*)
                v="${line#*Id=}"
                v="${v%%[!0-9]*}"
                if [ -n "$v" ] && [ "$v" -gt 0 ] 2>/dev/null; then
                    echo "$v"
                    break
                fi
                ;;
        esac
    done)
    if [ -n "$UID_DUMP" ]; then
        echo "$UID_DUMP"
        return 0
    fi

    # Метод 2: stat (если доступен и владелец не root)
    if UG_STAT=$(stat -c "%u" "$APP_DIR" 2>/dev/null) && [ "$UG_STAT" != "0" ] && [ -n "$UG_STAT" ]; then
        echo "$UG_STAT"
        return 0
    fi

    # Метод 3: парсинг числового UID из ls -lnd
    for arg in $(ls -lnd "$APP_DIR" 2>/dev/null); do
        case "$arg" in
            1[0-9][0-9][0-9][0-9]) # Диапазон UID непривилегированных приложений Android
                echo "$arg"
                return 0
                ;;
        esac
    done

    return 1
}

APP_UID=$(get_app_uid)
if [ -z "$APP_UID" ] || [ "$APP_UID" = "0" ]; then
    echo "ERROR: Failed to detect valid non-root UID for uk.nktnet.webviewkiosk" >&2
    exit 1
fi
UG="$APP_UID:$APP_UID"

# 2. Определение ориентации (сохраняем существующую или берем системную)
ROT="ROTATION_90"
if [ -f "$PREFS_DIR/user_settings.xml" ]; then
    # Потоковый парсинг чисто средствами mksh (без grep -o, sed, awk)
    while IFS= read -r line; do
        case "$line" in
            *name=\"device.rotation\"*)
                r="${line#*>}"
                r="${r%%<*}"
                case "$r" in
                    ROTATION_*) ROT="$r" ;;
                esac
                break
                ;;
        esac
    done < "$PREFS_DIR/user_settings.xml"
fi

# Если ориентация не была задана в файле, проверяем текущую системную ориентацию
if [ "$ROT" = "ROTATION_90" ]; then
    SYS_ROT=$(settings get system user_rotation 2>/dev/null || true)
    if [ "$SYS_ROT" = "3" ]; then
        ROT="ROTATION_270"
    fi
fi

# 3. Остановка киоска перед записью настроек
am force-stop uk.nktnet.webviewkiosk 2>/dev/null || true
sleep 1

# 4. Удаление старого бэкапа SharedPreferences во избежание отката со стороны ОС
rm -f "$PREFS_DIR/user_settings.xml.bak"

# 5. Атомарная запись настроек (через .tmp)
cat << EOF > "$PREFS_DIR/user_settings.xml.tmp"
<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<map>
    <string name="web_content.home_url">file:///sdcard/Download/informer.html</string>
    <boolean name="web_engine.allow_universal_access_from_file_urls" value="true" />
    <boolean name="web_engine.allow_file_access_from_file_urls" value="true" />
    <boolean name="web_engine.enable_dom_storage" value="true" />
    <boolean name="web_content.allow_local_files" value="true" />
    <string name="appearance.address_bar_mode">HIDDEN</string>
    <string name="appearance.floating_toolbar_mode">HIDDEN</string>
    <string name="appearance.immersive_mode">SYSTEM_BARS</string>
    <string name="appearance.theme">DARK</string>
    <string name="appearance.webview_inset">NONE</string>
    <string name="device.rotation">$ROT</string>
    <boolean name="device.keep_screen_on" value="true" />
</map>
EOF
mv -f "$PREFS_DIR/user_settings.xml.tmp" "$PREFS_DIR/user_settings.xml"

# 6. Если system_settings.xml отсутствует или пуст — создаём с историей informer.html
if [ ! -s "$PREFS_DIR/system_settings.xml" ]; then
cat << 'EOF' > "$PREFS_DIR/system_settings.xml.tmp"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="history_stack">[{&quot;id&quot;:&quot;kiosk-informer&quot;,&quot;url&quot;:&quot;file:///sdcard/Download/informer.html&quot;,&quot;visitedAt&quot;:1790000000000}]</string>
    <string name="intent_url"></string>
    <string name="app_instance_id">kiosk-auto-instance</string>
    <int name="history_index" value="0" />
</map>
EOF
mv -f "$PREFS_DIR/system_settings.xml.tmp" "$PREFS_DIR/system_settings.xml"
fi

# 7. Выставляем владельца, права и контекст SELinux
chown -R "$UG" "$PREFS_DIR" 2>/dev/null || true
chmod 771 "$APP_DIR" "$PREFS_DIR" 2>/dev/null || true
chmod 660 "$PREFS_DIR"/*.xml 2>/dev/null || true
restorecon -R "$PREFS_DIR" 2>/dev/null || restorecon "$PREFS_DIR"/* 2>/dev/null || true

# 8. Запуск киоска
sleep 1
am start -n uk.nktnet.webviewkiosk/.MainActivity >/dev/null 2>&1 || true
