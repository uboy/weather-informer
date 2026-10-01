#!/bin/bash
# Единый прогон всех тестов. ОБЯЗАТЕЛЕН перед деплоем (deploy/all-tablets.sh вызывает).
set -eo pipefail
cd "$(dirname "$0")/.."
echo "=== node icon-test ==="; node tests/icon-test.js | tail -1
echo "=== node converters-test ==="; node tests/converters-test.js | tail -1
echo "=== node ui-test ==="; node tests/ui-test.js | tail -1
echo "=== python gismeteo ==="; python3 tests/test_gismeteo.py 2>&1 | tail -1
echo "=== python gismeteo_v2 ==="; python3 tests/test_gismeteo_v2.py 2>&1 | tail -1
echo "=== python foreca ==="; python3 tests/test_foreca_provider.py 2>&1 | tail -1
echo "=== python synop ==="; python3 tests/test_synop.py 2>&1 | tail -1
echo "=== python accuracy ==="; python3 tests/test_accuracy.py 2>&1 | tail -1
echo "=== python history/7timer ==="; python3 tests/test_history.py 2>&1 | tail -1
echo "=== python fallback hierarchy ==="; python3 tests/test_fallback.py 2>&1 | tail -1
echo "=== ВСЁ ЗЕЛЁНОЕ ==="
