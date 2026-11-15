#!/bin/bash
# Деплой на весь парк: список серийников/IP в переменной или аргументах
set -e

# Тесты обязаны быть зелёными до деплоя
bash "$(dirname "$0")/../tests/run-all.sh"

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$DIR/.." && pwd)"
TABLETS="${@:-$(grep -vE '^[[:space:]]*(#|$)' "$DIR/tablets.list" 2>/dev/null || true)}"
[ -n "$TABLETS" ] || { echo "usage: $0 <serial|ip[:5555]> [...] или создай deploy/tablets.list"; exit 1; }
FAILS=0

for t in $TABLETS; do
  ip="${t%:*}"
  echo "=== Деплой на $t ==="
  deployed=0

  # 1. Пробуем быстро по Zero-ADB OTA HTTP (порт 8080) только если передан IP-адрес
  if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && curl -s -m 2 "http://$ip:8080/health" 2>/dev/null | grep -q '"app":"WeatherInformerKiosk"'; then
    echo "  [OTA HTTP] Обновление веб-файлов через http://$ip:8080/api/upload..."
    upload_ok=1
    for f in qrcode.min.js setup.html settings.html informer.html; do
      if [ -f "$ROOT_DIR/$f" ]; then
        resp=$(curl -s -m 8 -X POST -F "file=@$ROOT_DIR/$f" "http://$ip:8080/api/upload" 2>/dev/null || true)
        if ! echo "$resp" | grep -q '"status":"uploaded"'; then
          echo "    Ошибка загрузки $f: $resp"
          upload_ok=0
        fi
      fi
    done

    # Синхронизация боевых ключей и параметров на планшет
    CFG_FILE="$ROOT_DIR/config.json"
    [ -f "$CFG_FILE" ] || CFG_FILE="/home/dmazur/services/weather-informer/config.json"
    if [ -f "$CFG_FILE" ]; then
      echo "  [Config] Синхронизация ключей API на $ip из $CFG_FILE..."
      PAYLOAD=$(python3 -c "
import json
try:
    cfg = json.load(open('$CFG_FILE'))
    keys = {
        'api': cfg.get('api', ''),
        'gismeteo_api_key': cfg.get('gismeteo_api_key', ''),
        'foreca_api_key': cfg.get('foreca_api_key', ''),
        'openweathermap_api_key': cfg.get('openweathermap_api_key', ''),
        'server_url': (cfg.get('server_url') or '').strip() or 'http://192.168.1.55:8085/weather.json',
        'city_name': cfg.get('city_name', 'Нижний Новгород'),
        'lat': cfg.get('lat', 56.317722),
        'lon': cfg.get('lon', 43.999303),
        'station_altitude': cfg.get('station_altitude', 160.0),
        'primary_source': 'server'
    }
    print(json.dumps(keys))
except Exception as e:
    print('{}')
")
      if [ "$PAYLOAD" != "{}" ]; then
        cfg_resp=$(curl -s -m 5 -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "http://$ip:8080/api/settings" 2>/dev/null || true)
        echo "    Ответ сервера настроек: $cfg_resp"
      fi
    fi

    # Перезагрузка информера на экране
    curl -s -m 3 "http://$ip:8080/api/reload" >/dev/null 2>&1 || true

    if [ "$upload_ok" -eq 1 ]; then
      echo "  [OTA HTTP] $t: Успешно обновлено и синхронизировано"
      deployed=1
    fi
  fi

  # 2. Fallback через ADB (если HTTP не ответил)
  if [ "$deployed" -eq 0 ]; then
    echo "  [ADB] Попытка деплоя через ADB..."
    if "$DIR/informer-to-tablet.sh" "$t"; then
      echo "  [ADB] $t: Успешно обновлено"
      deployed=1
    else
      echo "  [ADB] $t: FAIL"
      FAILS=$((FAILS+1))
    fi
  fi
done

[ "$FAILS" -eq 0 ] || { echo "Деплой не прошёл на $FAILS планшетах"; exit 1; }
echo "Все целевые планшеты успешно обновлены!"
