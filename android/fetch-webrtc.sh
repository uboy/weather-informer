#!/usr/bin/env bash
# Скачивание и распаковка native org.webrtc prebuilt (фаза 1 камер).
# Источник: io.getstream:stream-webrtc-android:1.3.10, Maven Central.
# Извлекаются только classes.jar и lib/armeabi-v7a (весь флот - armeabi-v7a).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBRTC_DIR="$SCRIPT_DIR/webrtc"
AAR_URL="https://repo1.maven.org/maven2/io/getstream/stream-webrtc-android/1.3.10/stream-webrtc-android-1.3.10.aar"
AAR_SHA256="afa3b0feaa2902f6ece10e67ce3b7e6d9def18a86b73f0c6a524209cb938aa96"
AAR_FILE="$WEBRTC_DIR/stream-webrtc-android-1.3.10.aar"

mkdir -p "$WEBRTC_DIR"

if [ ! -f "$AAR_FILE" ]; then
    echo "Скачивание $AAR_URL"
    curl -fsSL -m 600 -o "$AAR_FILE" "$AAR_URL"
fi

actual_aar=$(sha256sum "$AAR_FILE" | cut -d' ' -f1)
if [ "$actual_aar" != "$AAR_SHA256" ]; then
    echo "ОШИБКА: sha256 AAR не совпал ($actual_aar)"
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
unzip -o -q "$AAR_FILE" classes.jar "jni/armeabi-v7a/*" -d "$WORK"

cp "$WORK/classes.jar" "$WEBRTC_DIR/classes.jar"
mkdir -p "$WEBRTC_DIR/armeabi-v7a"
cp "$WORK/jni/armeabi-v7a/libjingle_peerconnection_so.so" "$WEBRTC_DIR/armeabi-v7a/libjingle_peerconnection_so.so"

echo "Готово:"
sha256sum "$WEBRTC_DIR/classes.jar" "$WEBRTC_DIR/armeabi-v7a/libjingle_peerconnection_so.so"
