#!/usr/bin/env bash
# Скрипт быстрой и детерминированной сборки APK «Weather Informer Kiosk & Server»
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

BUILD_DIR="$SCRIPT_DIR/build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/dex"

echo "=== 1. Копирование свежих ассетов информера ==="
cp "$ROOT_DIR/informer.html" "$ASSETS_DIR/informer.html"
cp "$ROOT_DIR/jquery.min.js" "$ASSETS_DIR/jquery.min.js"
if [ -f "$ROOT_DIR/config.json" ]; then
    cp "$ROOT_DIR/config.json" "$ASSETS_DIR/config.json"
fi

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

echo "=== 4. Компиляция Java классов (javac) ==="
javac -g -encoding UTF-8 \
    -source 1.8 -target 1.8 \
    -cp "$ANDROID_JAR" \
    -d "$BUILD_DIR/classes" \
    "$BUILD_DIR/gen/ru/weather/informer/R.java" \
    "$SRC_DIR/ru/weather/informer/"*.java

echo "=== 5. Трансляция байткода в Dalvik DEX (d8) ==="
"$D8" --lib "$ANDROID_JAR" \
    --min-api 21 \
    --output "$BUILD_DIR/dex" \
    "$BUILD_DIR/classes/ru/weather/informer/"*.class

echo "=== 6. Добавление classes.dex в APK ==="
cd "$BUILD_DIR/dex"
zip -uj "$BUILD_DIR/unaligned_raw.apk" classes.dex
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
