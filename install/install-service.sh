#!/bin/bash
# Установка systemd --user сервиса погодного сервера
# Запускать из корня репозитория на хосте, где будет жить сервер
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
[ -f "$DIR/config.json" ] || { echo "Нет $DIR/config.json — скопируй config.example.json в config.json и впиши ключ"; exit 1; }
mkdir -p "$UNIT_DIR"
sed "s|/home/dmazur|$HOME|g" "$DIR/install/weather-informer.service" > "$UNIT_DIR/weather-informer.service"
systemctl --user daemon-reload
systemctl --user enable --now weather-informer
# автостарт юнита без входа пользователя в систему (сервер без логина)
loginctl enable-linger "$USER" 2>/dev/null || true
sleep 3
systemctl --user status weather-informer --no-pager | head -5
curl -s -m 5 "http://127.0.0.1:$(grep -oP '(?<="port": )\d+' "$DIR/config.json")/status" && echo " — сервер работает"
