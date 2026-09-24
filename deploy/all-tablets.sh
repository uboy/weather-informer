#!/bin/bash
# Деплой на весь парк: список серийников/IP в переменной или аргументах
set -e
DIR="$(cd "$(dirname "$0")")"
TABLETS="${@:-$(cat "$DIR/tablets.list" 2>/dev/null)}"
[ -n "$TABLETS" ] || { echo "usage: $0 <serial|ip:5555> [...] или создай deploy/tablets.list"; exit 1; }
for t in $TABLETS; do "$DIR/informer-to-tablet.sh" "$t"; done
