# MEMORY.md — SBER Padel Tour

## Проект
- Платформа падел-турнира: статический сайт (GitHub Pages, sber-padel-tour.ru) + Firebase (Firestore, Cloud Functions callable v2, region europe-west1) + Telegram-бот на этом сервере (`bot_venv`).
- Репо сайта: `sergistanbul22/sber-padel-tour` (токен в remote URL). Деплой: `index_sberpt.html` → копия в `index.html` → push.
- Репо функций: `/root/.openclaw/workspace/sberpt-functions` (ветка master).
- Пользователь работает с двух машин → **перед push всегда fetch и сверка с origin/main**.
- **Пуш в GitHub — только после явного «ок» пользователя** (правило от 2026-09-17, подтверждено дважды).

## Архитектура рейтинга (после 2026-09-16/17)
- Рейтинг считают ТОЛЬКО Cloud Functions (finalizeTournament, finalizeGame, recalculateTournament, recalculateAllResults, deleteTournament, deleteGame, archiveRatings).
- Математика — `functions/src/algorithm`, parity с эталоном сайта подтверждён `test/parity.test.js` (37 тестов). Эталон заморожен в `test/fixtures/site-algorithm.js`.
- Из сайта вся локальная математика удалена (2026-09-17): сайт только вызывает `callCloudFn`.
- Firestore Rules: рейтинговые поля (mu/sigma/rating/medals/tsChanges/ratingChanges/счётчики) меняют только функции (Admin SDK) и админы вручную; create игрока — только с дефолтными mu=25/sigma≈8.33/rating=0; settings и planned_games — write для signed-in (был старый баг с двумя захардкоженными email). 38 rules-тестов на эмуляторе.

## Ключевые окружения
- env для деплоя: `GOOGLE_APPLICATION_CREDENTIALS=/root/.openclaw/workspace/sberpt-firebase-key.json`
- Скрипты: `deploy_rules.sh`, `deploy_functions.sh`, `start_bot.sh`
- Тесты функций: `cd sberpt-functions/functions && node test/parity.test.js | firestore.rules.test.js | integration.test.js`
- Супер-админ: `6585875box@sberpadel.local`; тестовые email-аккаунты `*@sberpadel.local`.

## Турнирный vs игровой рейтинг
- Турнирный ratingChanges = Σ w (сырой muDelta), игровой = newRating − oldRating (int) — семантика разная by design.

## Уроки
- rules-unit-testing v5: `withSecurityRulesDisabled(cb)` передаёт контекст → нужен `ctx.firestore()`; email-claim в `authenticatedContext(uid, {email})`.
- Тестовые данные в parity/rules тестах — только синтетические email в зоне `.sberpadel.local` (ранее тесты трогали реальные аккаунты).
- **firebase-firestore-compat.js 9.22 НЕ имеет `Query.count()`** (агрегации — только модульный API). Счётчики сайта — через `padel_counters/games` (ведут функции: finalizeGame +1 / deleteGame −1 / recalculateAllResults абсолют). Обнаружено 2026-09-18, фикс: commits `6c92790` (functions) + `a0a6a22` (site).
- Admin SDK v12: FieldValue из `firebase-admin/firestore`; в tx для счётчиков — `set(merge:true)`, не `update`.

## Операционка (актуально 2026-09-21)
- **Бот турнира живёт под systemd: `sber-padel-bot.service`** — рестарт/статус только через `systemctl`. Ручной `start_bot.sh` конфликтует с юнитом (дубли процессов, 409 Conflict на getUpdates). Рядом аналогичные юниты: `sber-padel-reset.service` (password_reset_server.py), `wc2026-bot.service`.
- **Инцидент с вебхуком (2026-09-21)**: на токене бота обнаружен чужой webhook `https://tele.goldenherd.com/tg/webhook/8763865911` (502, pending 28) — из-за него polling бота месяцами получал 409 и команды (/mode, /setpassword) не работали. Webhook удалён через deleteWebhook, polling восстановлен. **Токен скомпрометирован — рекомендована ротация через @BotFather (/revoke)**; токен захардкожен в tournament_bot.py и мог утекать через письма/логи.
- `check_single_instance()` в боте переписана на скан /proc (старый pgrep -f матчил bash-обёртки → ложные «уже запущен» и crash-loop systemd, счётчик рестартов был 2409).

## Уведомления организаторам тренировок (2026-09-21)
- Новая коллекция `telegram_links` (doc id = @username lowercase, поля chat_id/updatedAt). Пишет только бот (Admin SDK), rules — default deny.
- Бот запоминает @username→chat_id из КАЖДОГО входящего сообщения (polling) и дублирует уведомления о заявках/записях на тренировки организатору в ЛС (resolve: training.createdBy → users/{email}.telegram → telegram_links).
- Дубль админу не шлётся, если organizer == ADMIN_CHAT_ID; в test-режиме бота копия идёт админу с пометкой.
- Условие для организатора: в профиле сайта заполнен @username + хоть раз написал боту. По состоянию на 21.09 telegram в профиле заполнили 31 пользователь.
