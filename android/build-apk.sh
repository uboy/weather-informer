#!/usr/bin/env bash
# Скрипт быстрой и детерминированной сборки APK «Weather Informer Kiosk & Server»
# Фаза 1 камер (2026-11-21): добавлена упаковка native org.webrtc (classes.jar -> d8,
# libjingle_peerconnection_so.so -> jniLibs/armeabi-v7a). Источник prebuilt:
# io.getstream:stream-webrtc-android:1.3.10 (Maven Central), ABI armeabi-v7a для всего флота.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ANDROID_HOME="${ANDROID_HOME:-/home/dmazur/Android/Sdk}"
PLATFORM_DIR="$ANDROID_HOME/platforms/android-33"
BUILD_TOOLS_DIR="$ANDROID_HOME/build-tools/34.0.0"

ANDROID_JAR="$PLATFORM_DIR/android.jar"
AAPT2="$BUILD_TOOLS_DIR/aapt2"
D8="$BUILD_TOOLS_DIR/d8"
ZIPALIGN="$BUILD_TOOLS_DIR/zipalign"
APKSIGNER="$BUILD_TOOLS_DIR/apksigner"

APP_DIR="$SCRIPT_DIR/app"
SRC_DIR="$APP_DIR/src/main/java"
RES_DIR="$APP_DIR/src/main/res"
ASSETS_DIR="$APP_DIR/src/main/assets"
MANIFEST="$APP_DIR/src/main/AndroidManifest.xml"

# Native WebRTC prebuilt (armeabi-v7a): classes.jar + libjingle_peerconnection_so.so
WEBRTC_DIR="$SCRIPT_DIR/webrtc"
WEBRTC_CLASSES_JAR="$WEBRTC_DIR/classes.jar"
WEBRTC_SO="$WEBRTC_DIR/armeabi-v7a/libjingle_peerconnection_so.so"
WEBRTC_AAR="$WEBRTC_DIR/stream-webrtc-android-1.3.10.aar"
WEBRTC_AAR_SHA256="afa3b0feaa2902f6ece10e67ce3b7e6d9def18a86b73f0c6a524209cb938aa96"

BUILD_DIR="$SCRIPT_DIR/build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/dex"

echo "=== 0. Проверка native WebRTC prebuilt ==="
if [ ! -f "$WEBRTC_CLASSES_JAR" ] || [ ! -f "$WEBRTC_SO" ]; then
    echo "webrtc/classes.jar или webrtc/armeabi-v7a/libjingle_peerconnection_so.so отсутствуют - распаковка из AAR"
    bash "$SCRIPT_DIR/fetch-webrtc.sh"
fi
if [ -f "$WEBRTC_AAR" ]; then
    actual_aar=$(sha256sum "$WEBRTC_AAR" | cut -d' ' -f1)
    if [ "$actual_aar" != "$WEBRTC_AAR_SHA256" ]; then
        echo "ОШИБКА: sha256 AAR не совпал ($actual_aar), сборка остановлена"
        exit 1
    fi
fi

echo "=== 1. Копирование свежих ассетов информера ==="
cp "$ROOT_DIR/informer.html" "$ASSETS_DIR/informer.html"
cp "$ROOT_DIR/jquery.min.js" "$ASSETS_DIR/jquery.min.js"
cp "$ROOT_DIR/qrcode.min.js" "$ASSETS_DIR/qrcode.min.js"
cp "$ROOT_DIR/setup.html" "$ASSETS_DIR/setup.html"
cp "$ROOT_DIR/settings.html" "$ASSETS_DIR/settings.html"
# Не копируем $ROOT_DIR/config.json, чтобы боевые ключи не попадали в APK

echo "=== 2. Компиляция ресурсов (aapt2 compile) ==="
mkdir -p "$BUILD_DIR/compiled_res"
"$AAPT2" compile --dir "$RES_DIR" -o "$BUILD_DIR/compiled_res.zip"

echo "=== 3. Линковка ресурсов и генерация R.java (aapt2 link) ==="
"$AAPT2" link \
    -I "$ANDROID_JAR" \
    --manifest "$MANIFEST" \
    --java "$BUILD_DIR/gen" \
    -A "$ASSETS_DIR" \
    -o "$BUILD_DIR/unaligned_raw.apk" \
    "$BUILD_DIR/compiled_res.zip"

echo "=== 4. Компиляция Java классов (javac, classpath += webrtc classes.jar) ==="
javac -g -encoding UTF-8 \
    -source 1.8 -target 1.8 \
    -cp "$ANDROID_JAR:$WEBRTC_CLASSES_JAR" \
    -d "$BUILD_DIR/classes" \
    "$BUILD_DIR/gen/ru/weather/informer/R.java" \
    "$SRC_DIR/ru/weather/informer/"*.java

echo "=== 5. Трансляция байткода в Dalvik DEX (d8: приложение + org.webrtc) ==="
# d8 для min-api 21 сам делает native multidex (classes.dex, classes2.dex, ...)
"$D8" --lib "$ANDROID_JAR" \
    --min-api 21 \
    --output "$BUILD_DIR/dex" \
    "$WEBRTC_CLASSES_JAR" \
    $(find "$BUILD_DIR/classes" -name '*.class')

echo "=== 6. Добавление dex-файлов и jniLibs (armeabi-v7a) в APK ==="
cd "$BUILD_DIR/dex"
for d in "$BUILD_DIR"/dex/*.dex; do
    zip -uq "$BUILD_DIR/unaligned_raw.apk" "$(basename "$d")"
done
cd "$BUILD_DIR"
mkdir -p lib/armeabi-v7a
cp "$WEBRTC_SO" lib/armeabi-v7a/libjingle_peerconnection_so.so
zip -u "$BUILD_DIR/unaligned_raw.apk" lib/armeabi-v7a/libjingle_peerconnection_so.so
cd "$SCRIPT_DIR"

echo "=== 7. Выравнивание APK (zipalign) ==="
"$ZIPALIGN" -f -p 4 "$BUILD_DIR/unaligned_raw.apk" "$BUILD_DIR/aligned.apk"

echo "=== 8. Подпись APK (apksigner) ==="
KEYSTORE="$SCRIPT_DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    echo "Генерация debug keystore..."
    keytool -genkey -v -keystore "$KEYSTORE" \
        -storepass android -alias androiddebugkey -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=WeatherInformer,O=Local,C=RU"
fi

"$APKSIGNER" sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --key-pass pass:android \
    --out "$ROOT_DIR/apks/informer.apk" \
    "$BUILD_DIR/aligned.apk"

echo "=== ГОТОВО: $ROOT_DIR/apks/informer.apk ==="
ls -lh "$ROOT_DIR/apks/informer.apk"
