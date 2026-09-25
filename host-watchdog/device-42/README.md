# Скрипты на планшете .42 (Digma Plane 7514S)

Живут в `/data/adb/service.d/` (Magisk, выполняются при каждой загрузке).
Копия в репо — резерв от стирания /data (ROM .42 известен сбросами persist).

- `99-adbtcp.sh` — adb-over-WiFi: persist-проперти 5555 (ROM их стирает),
  рестарт adbd, быстрый разгон экрана/ключгарда первые 15 мин после загрузки,
  ежедневный рефреш adb в 04:00.
- `kg_watch.sh` — бессрочный сторож ключгарда (раз в минуту, только если
  `isStatusBarKeyguard=true`; Digma-keyguard на swipe не реагирует — только
  keyevent 82 + wm dismiss-keyguard). Демон до first-unlock не срабатывает
  (SELinux/окружение) — после загрузки нужен один ручной dismiss, дальше
  держит сам. Страховка владельца: экран блокировки выключен в настройках.

## informer_autostart.sh (на ВСЕХ планшетах)

Владельческий автостарт: ждёт boot_completed, затем
`settings put global stay_on_while_plugged_in 7` + `svc power stayon true`
(не гасить экран при питании). С 2026-09-25 развёрнут на все 5 планшетов.
На .20/.30/.31/t5(.23) — root без Magisk: каталог service.d создан вручную;
основная гарантия — глобальная настройка stay_on_while_plugged_in=7 (переживает ребут).
