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

  # 1. Пробуем быстро по Zero-ADB OTA HTTP (порт 8080)
  if curl -s -m 2 "http://$ip:8080/health" 2>/dev/null | grep -q '"app":"WeatherInformerKiosk"'; then
    echo "  [OTA HTTP] Обновление через http://$ip:8080/api/upload..."
    resp=$(curl -s -m 8 -X POST -F "file=@$ROOT_DIR/informer.html" "http://$ip:8080/api/upload" 2>/dev/null || true)
    if echo "$resp" | grep -q '"status":"uploaded"'; then
      curl -s -m 4 "http://$ip:8080/api/reload" >/dev/null 2>&1 || true
      echo "  [OTA HTTP] $t: Успешно загружено и перезагружено"
      deployed=1
    else
      echo "  [OTA HTTP] Ошибка загрузки: $resp (пробуем ADB fallback)"
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
