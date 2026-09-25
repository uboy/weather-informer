# Watchdogs — ОТКЛЮЧЕНЫ (решение владельца, 2026-09-25)

`park-adb-watchdog.timer` остановлен и disabled на хосте; на .42 удалены
`/data/adb/service.d/99-adbtcp.sh` и `kg_watch.sh` (демоны убиты).
adb-переподключение — вручную владельцем.

Файлы оставлены в репо как справка:
- `park-adb-watchdog.sh` — хостовой adb-reconnect (бывший таймер 3 мин)
- `device-42/` — копии скриптов, живших на .42 (99-adbtcp: persist adb 5555 —
  ROM Digma стирает проперти; kg_watch: сторож ключгарда)

ВНИМАНИЕ: без 99-adbtcp.sh после перезагрузки .42 adb-over-WiFi не поднимется
(ROM стирает persist.adb.tcp.port) — вернуть: USB + `setprop persist.adb.tcp.port 5555`.
