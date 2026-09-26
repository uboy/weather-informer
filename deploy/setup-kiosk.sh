#!/bin/sh
# Настройка WebView Kiosk на Android-устройстве.
# Запускается под root (через su -c или напрямую).
set -e

APP_DIR="/data/data/uk.nktnet.webviewkiosk"
if [ ! -d "$APP_DIR" ]; then
    # Киоск не установлен — выходим без ошибки
    exit 0
fi

PREFS_DIR="$APP_DIR/shared_prefs"
mkdir -p "$PREFS_DIR"

UG=$(stat -c "%u:%g" "$APP_DIR" 2>/dev/null || echo "0:0")

# Сохраняем текущую ориентацию экрана, если была настроена
ROT="ROTATION_90"
if [ -f "$PREFS_DIR/user_settings.xml" ]; then
    EXISTING_ROT=$(grep -o 'ROTATION_[0-9]*' "$PREFS_DIR/user_settings.xml" 2>/dev/null | head -n 1)
    if [ -n "$EXISTING_ROT" ]; then
        ROT="$EXISTING_ROT"
    fi
fi

# Остановка киоска перед записью настроек SharedPreferences
am force-stop uk.nktnet.webviewkiosk 2>/dev/null || true

cat << EOF > "$PREFS_DIR/user_settings.xml"
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

# Если system_settings.xml отсутствует или пуст — создаём с историей informer.html
if [ ! -s "$PREFS_DIR/system_settings.xml" ]; then
cat << 'EOF' > "$PREFS_DIR/system_settings.xml"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="history_stack">[{&quot;id&quot;:&quot;kiosk-informer&quot;,&quot;url&quot;:&quot;file:///sdcard/Download/informer.html&quot;,&quot;visitedAt&quot;:1790000000000}]</string>
    <string name="intent_url"></string>
    <string name="app_instance_id">kiosk-auto-instance</string>
    <int name="history_index" value="0" />
</map>
EOF
fi

# Выставляем владельца, права и контекст SELinux
chown "$UG" "$PREFS_DIR" "$PREFS_DIR"/*.xml 2>/dev/null || true
chmod 771 "$PREFS_DIR" 2>/dev/null || true
chmod 660 "$PREFS_DIR"/*.xml 2>/dev/null || true
restorecon -R "$PREFS_DIR" 2>/dev/null || true

# Запуск киоска
sleep 1
am start -n uk.nktnet.webviewkiosk/.MainActivity >/dev/null 2>&1 || true
