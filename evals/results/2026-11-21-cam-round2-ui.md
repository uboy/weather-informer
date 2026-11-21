# Eval: камерный раунд-2 UI + безопасность settings (2026-11-21)

Target: caching_server.py (GET /api/settings cameras_rooms), dashboard.html, settings.html.
Коммиты: 75ceb08, 84da00f, b730a67, 4670c7e (ветка feature/weather-informer-cameras).

## Позитивные кейсы (реальный прогон)

1. Честный no_publisher UX: клик «Смотреть» на spalnya-okno (без паблишера) на дев-8099
   через браузер (IAB): pstatus = «сигналинг недоступен (переходная схема) - живое видео
   появится с pull-моделью; показываю свежий снапшот», #pmjpeg видим, naturalWidth=640.
   Скриншот: ~/runs/weather-informer-cam-phase1-mvp/logs/dash-honest-msg-snapshot.png.
2. Путь успеха не изменён: «Смотреть» на gostinaya (издатель жив) → pstatus «видео:
   соединение установлено (connected)», #pvideo видим, #pmjpeg скрыт. Ориентация гостиной
   (rotate(180deg) scaleX(-1)) применена к живому видео.
3. Удаление с плитки: временная комната zzdel (добавлена через POST /api/cam/rooms),
   клик ✕ на плитке (confirm→OK): adm_msg «ок: remove zzdel», сетка/ROOMS/config.json
   вернулись к 4 реальным комнатам (no-loss). Кнопки удаления из админ-секции убраны
   (served HTML: «удалить: » = 0), confirm() в коде плитки сохранён (grep «Удалить комнату»=1).
4. Ориентация: ⟳×2 + ⇄ на плитке гостиной → localStorage camrot=180, camflip=1,
   transform preview img «rotate(180deg) scaleX(-1)»; ПЕРЕЗагрузка страницы → transform
   восстановлен на свежесозданном img (фикс b730a67: applyOrientation в refreshSnap).
   Скриншот: logs/dash-tiles-orientation.png (✕/⟳/⇄ на всех плитках).
5. Порт-гейт ссылки Дашборд (settings.html): prod 8085 отдаёт новую разметку
   (cam_dash_link=4 совпадения, старый `href="/dashboard"` = 0); dev 8099 — относительная
   ссылка «(дев)»; cameras_rooms в GET /api/settings: 4 комнаты с tablet, has_token=False.

## Негативные кейсы (обязаны падать по своей причине)

1. Утечка tablet_token через GET /api/settings — тест
   test_settings_cameras_rooms_no_token_leak (AssertNotIn "tablet_token"): если секция
   когда-нибудь отдаст токен, тест падает с явной причиной «tablet_token must not leak».
   Прогон: 15/15 passed (pytest tests/test_cam_ws.py, 0.71s, dev-хост asrock).
2. Auth-негативы сохранены: dashboard/summary/rooms без Basic → 401, viewer-token без
   auth → 401 (tests 343-400, в прогоне 15/15).

## Вывод

Владельческие сценарии (просмотр без сигналинга, удаление комнаты, поворот кадра,
переход на дашборд с серверной страницы) работают E2E; секреты (tablet_token) через
settings API не утекают; киоск-краш NPE (CamSignalingClient.sendControl) закрыт guard'ом
в 4670c7e, APK-цикл с ним — следующий шаг (выкат флота только после предупреждения
координатора).
