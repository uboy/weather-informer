#!/bin/bash
# Деплой на весь парк: список серийников/IP в переменной или аргументах
set -e

# Тесты обязаны быть зелёными до деплоя
bash "$(dirname "$0")/../tests/run-all.sh"

DIR="$(cd "$(dirname "$0")")"
TABLETS="${@:-$(cat "$DIR/tablets.list" 2>/dev/null)}"
[ -n "$TABLETS" ] || { echo "usage: $0 <serial|ip:5555> [...] или создай deploy/tablets.list"; exit 1; }
for t in $TABLETS; do "$DIR/informer-to-tablet.sh" "$t" || echo "$t: FAIL"; done
