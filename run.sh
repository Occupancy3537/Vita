#!/bin/bash
# card-service — Phase 0. Публично не торчит: порт только на 127.0.0.1, сеть pgnet
# (та же, что у pg и n8n — см. backups/infra/pg_run.sh в основном репозитории).
# Волна 1 (A3, 2026-09-17): TELEGRAM_POLLING_ENABLED закреплён
# (CAPITAN_RELAY_URL убран 2026-09-21, #38/#43 — "other" теперь идёт в /ingest
# в процессе, не на внешний релей, см. app/doctor/poller.py::ingest_test_message)
# Хотфикс 17.09 (ZCode): + DASHBOARD_TOKEN — без него /dashboard/* отдаёт 403 fail-closed
# 2026-09-21 (#38/#45): + WIDGET_TOKEN — /widget/nutrition-diary, тот же fail-closed
# паттерн (значение раньше жило в коде main.py, теперь только в окружении, как и
# остальные секреты в этом списке — задать в шелле ДО запуска этого скрипта).
# + ERR_DEDUP_TOKEN — /err-dedup, значение то же, что уже читают три ночных
# cron-скрипта из backups/infra/.google_oauth.env (google_oauth_creds.js)
# 2026-09-21 (по прямому запросу Влада, ИСТОРИЯ): + HERMES_BOT_TOKEN
# (@Hermes_AI_vvk_bot) — системные алерты шли через TELEGRAM_BOT_TOKEN (бот
# доктора), регрессия миграции с n8n, где эти же воркфлоу слали через отдельный
# credential "Hermes Agent". 2026-09-24 (тикет «раскладка ботов по тематическим
# чатам»): УБРАН — Hermes-бот конфликтовал с личным ИИ-агентом Влада (NousPortal
# перехватывал эти сообщения как команды себе), полностью исключён из проекта.
# + NUTRITION_BOT_TOKEN (@vvk_gemini_bot) — раньше отчёты о питании (в n8n шли
# через credential "Отчет по питанию"), с 2026-09-24 репурпose-нут в общий
# сервисный бот app/notify.py (алерты + вечерний дайджест) — см.
# app/service_telegram.py. Отчёты о питании переехали на FOOD_DIARY_BOT_TOKEN
# (тот же бот, что дневник питания, см. app/food_diary_telegram.py).
# ROADMAP 5.5 (2026-09-24) → правка тем же днём (тикет выше): DIGEST_SCHEDULER_ENABLED
# — вечерний дайджест (app/digest.py, 21:50 ВЛ), теперь ТОЛЬКО «остальное»
# (жёлтые аномалии, weekly/monthly-отчёты, critical сверх бюджета) через
# сервисный бот. ANAMNESIS_SCHEDULER_ENABLED — анамнез вернулся на свой
# отдельный планировщик (11:00 ВЛ, чат ДОКТОРА) — первые сутки общего дайджеста
# показали, что анамнез внутри него неудобен.
# Волна 3 (B2): REGISTRAR_MODEL / REGISTRAR_PDF_MODEL — vision-модели регистратора
# лаб-документов (app/registrar.py; PDF идёт отдельной моделью — glm-5.3-flash не
# принимает file-модальность на OpenRouter, дефолт = модель фото-пути Food diary).
# здесь — без флага run.sh молча запускал контейнер С ВЫКЛЮЧЕННЫМ telegram-poller
# (main.py включает поток только по явному env; прод-режим = polling ON).
# 2026-09-19: SYSTEM_CHECK_ENABLED — порт n8n _System Check (app/system_check.py,
# ежедневно 08:43 VL). Причина переноса именно этого воркфлоу первым — он же был
# самым прожорливым по памяти активным узлом (без-лимита SELECT * по двум таблицам
# целиком каждое утро), см. докстринг модуля и STATE.md/AGENT_SYNC.md #20.
# 2026-09-20: GATE_WATCH_ENABLED — алерт на снятие/возврат гейта нагрузки
# (app/gate_watch.py, каждые 15 мин), порт из today-dashboard Build Today JSON
# (A6, ревью Opus 5 2026-09-09) — снятие мед-ограничения при грыже L5/S1
# обязано быть шумным.
# 2026-09-20: SMALL_ALERTS_ENABLED — планировщики app/memory_archive_check.py
# (ежедневно 08:00 VL) и app/backup_alert.py (ежедневно 09:00 UTC) — порты
# n8n _Memory Pre-Archive Check и _Backup Alert.
# 2026-09-22: DIET_TAGGER_ENABLED/app/diet_tagger.py удалён — по запросу
# Влада слит в app/food_diary.py: NOVA/veg_g/... теперь классифицируются в
# том же LLM-вызове, что и нутриенты, а не отдельным проходом раз в 15 мин.
# 2026-09-20: HEALTH_WATCHDOG_ENABLED — app/health_watchdog.py (ежедневно
# 09:00 VL), порт n8n Health Watchdog.
# 2026-09-20: NUTRITION_REPORTS_ENABLED — app/nutrition_reports.py (ежедневно
# 21:45 VL + еженедельно вс 12:00 VL), порт n8n Reports (дневной путь) +
# Weekly Food Report.
# 2026-09-20: WEEKLY_ADVISOR_ENABLED — app/weekly_advisor.py (еженедельно
# вс 20:00 VL), порт n8n Weekly AI Advisor. Крупнейший и последний порт
# группы 2 (26 нод n8n, ~760 строк JS). health.recommendations_log теперь
# пишется напрямую в Postgres (DELETE+INSERT по Date), не в Sheets.
# 2026-09-21: MEDS_FROM_CALENDAR_ENABLED — app/meds_from_calendar.py
# (ежедневно 09:00 VL), порт n8n Card: Meds from Calendar. Использует тот же
# Calendar-credential, что и группа 3 (1/2).
# 2026-09-21: MONTHLY_TREND_ENABLED — app/monthly_trend.py (1-е число месяца,
# 10:00 VL), порт n8n Monthly_Trend_Wellness — закрывает группу 1 целиком.
# 2026-09-23: ISSUE_REVIEW_ENABLED — app/issue_review.py (по воскресеньям
# 19:00 VL) — еженедельный дайджест нерешённых НЕ-критичных находок
# card.issue_log. Молчит, если решать нечего (см. её докстринг) — не новый
# постоянный источник шума, ответ на прямую просьбу Влада после инцидента с
# ложными предупреждениями на «Настройках».
# 2026-09-21: FOOD_DIARY_BOT_ENABLED + FOOD_DIARY_BOT_TOKEN — порт n8n
# Food diary_v5 (последний воркфлоу миграции). Свой бот "vlad_health"
# (credential 8CKBKo8CXTaLD3YI в n8n), СВОЙ long-polling цикл — по решению
# Влада опрос, не вебхук, тот же принцип, что у доктора (Hermes Agent),
# просто другой токен/поток. Sheets-дубль записи Meals оставлен навсегда
# (решение Влада), не только на переходный период.
# 2026-09-21: CARD_PROCESSOR_ENABLED — при проверке "можно ли убрать n8n"
# нашли, что собственная очередь /process card-service крутилась n8n-
# воркфлоу Card Processor (опрос раз в 5 мин) — реальный архитектурный
# пробел (если бы n8n остановился, врач тихо переставал бы обрабатывать
# сообщения), не просто перенос фичи. app/card_processor.py.
# 2026-09-21: PHENOAGE_CALC_ENABLED (раз в неделю, вс 09:00 VL) и
# YANDEX_CLIMATE_ENABLED + YANDEX_IOT_TOKEN (раз в час) — последние два
# реальных бизнес-воркфлоу n8n, найденные и перенесённые в том же заходе.
# После них в n8n остаётся только инфраструктурная обвязка.
# 2026-09-20: ANOMALY_DETECTOR_ENABLED + CARD_GOOGLE_*/RESCUETIME_API_KEY —
# группа 3 (1/2, вместе с Collect_Biohacking_Data -> POST /ingest/biohacking,
# см. app/biohacking_ingest.py). CARD_GOOGLE_CLIENT_ID/SECRET общие для
# Sheets и Calendar (один OAuth-клиент); refresh_token у каждого свой —
# Calendar-креды отдельно авторизованы Владом на перенос (более
# чувствительные данные, чем ячейки таблицы). RESCUETIME_API_KEY раньше
# лежал открытым текстом в параметрах n8n-ноды — вынесен в переменную
# окружения (гигиена, не поведенческое отличие).
# 2026-09-23 (аудит логики, "петля исходов"): RECOMMENDATIONS_EVAL_ENABLED —
# app/recommendations.py::run_scheduler(), раз в сутки считает вердикт для
# рекомендаций, у которых закрылось окно оценки. Движок (verdict_engine)
# существовал давно, но ничто его не вызывало — единственный способ получить
# вердикт был дёрнуть /recommendations/{id}/evaluate руками.
# 2026-09-25: RESEARCH_SCAN_ENABLED — «научный контур» (app/research_scan.py,
# по воскресеньям 21:50 ВЛ) — скан PubMed/ClinicalTrials.gov/medRxiv по темам
# профиля (card.research_topic), новых секретов не требует (публичные API +
# уже существующий OPENROUTER_API_KEY только для фильтра релевантности).
# 2026-09-25: CONSILIUM_SCHEDULER_ENABLED — «консилиум специалистов»
# (app/consilium.py), полный ежемесячный прогон 1-го числа ~09:30 ВЛ (весь
# профиль, без конкретного вопроса). Команда "/консилиум <тема>" в чате
# доктора — отдельный путь (app.consilium.submit_command, свой executor,
# не через это расписание); новых секретов не требует (переиспользует
# OPENROUTER_API_KEY, включая режим web-поиска плагином OpenRouter).
# 2026-09-26: PROBLEM_MAINTENANCE_ENABLED — «детектив» (app/problem.py),
# ежедневно 09:10 ВЛ: эпизоды без активности >30 дней -> presumed_resolved.
# Create/Close_Problem — по команде доктора, не по расписанию; новых секретов не требует.
# 2026-09-21 (#38/#46, аудит ZCode): set -e без -u пропускал незаданную
# переменную молча — `-e CARD_PG_PASSWORD=""` собирает контейнер, /health
# отдаёт 200, а сбой (пустой пароль БД, пустой токен бота и т.п.) всплывает
# только там, где переменная реально используется — часто не сразу и не
# очевидно откуда. `${VAR:?...}` ниже ловит и незаданную, и пустую (не
# только незаданную, как дал бы один set -u) — до сборки образа, с понятным
# сообщением какая именно переменная пуста.
set -euo pipefail
cd "$(dirname "$0")"

: "${CARD_PG_PASSWORD:?не задан — пароль card_service в Postgres}"
: "${OPENROUTER_API_KEY:?не задан — без него доктор/советник/регистратор молча не отвечают}"
: "${TELEGRAM_BOT_TOKEN:?не задан — бот доктора не сможет поллить Telegram}"
: "${FOOD_DIARY_BOT_TOKEN:?не задан — бот дневника питания не сможет поллить Telegram}"
: "${YANDEX_IOT_TOKEN:?не задан — сбор климата (Get Yandex Climate_2) не сможет авторизоваться}"
: "${CARD_GOOGLE_CLIENT_ID:?не задан — общий OAuth-клиент Sheets/Calendar}"
: "${CARD_GOOGLE_CLIENT_SECRET:?не задан — общий OAuth-клиент Sheets/Calendar}"
: "${CARD_GOOGLE_SHEETS_REFRESH_TOKEN:?не задан — Sheets-дубль Meals/Daily_Trends/дневного климата отвалится молча}"
: "${CARD_GOOGLE_CALENDAR_REFRESH_TOKEN:?не задан — Meds from Calendar не сможет читать календарь}"
: "${RESCUETIME_API_KEY:?не задан — Anomaly_Detector/Collect_Biohacking_Data потеряют этот источник}"
: "${DASHBOARD_TOKEN:?не задан — все /dashboard/* эндпоинты уйдут в fail-closed 403}"
: "${WIDGET_TOKEN:?не задан — /widget/nutrition-diary уйдёт в fail-closed 403}"
: "${ERR_DEDUP_TOKEN:?не задан — три ночных cron-скрипта не смогут слать алерты через /err-dedup}"
: "${NUTRITION_BOT_TOKEN:?не задан — сервисный бот (алерты+дайджест, @vvk_gemini_bot) не сможет слать}"
: "${ACTION_ACK_TOKEN:?не задан — кнопки 'сделал' на дашборде уйдут в fail-closed forbidden}"
: "${BACKUP_STATUS_TOKEN:?не задан — пинг ночного бэкапа не сможет обновить состояние}"

sudo docker build -t card-service:latest .

sudo docker rm -f card-service 2>/dev/null || true
sudo docker run -d \
  --name card-service \
  --restart unless-stopped \
  --network pgnet \
  --memory=192m \
  -p 127.0.0.1:8080:8080 \
  -e CARD_PG_HOST=pg \
  -e CARD_PG_PORT=5432 \
  -e CARD_PG_USER=card_service \
  -e CARD_PG_PASSWORD="$CARD_PG_PASSWORD" \
  -e CARD_PG_DATABASE=health \
  -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
  -e TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  -e TELEGRAM_POLLING_ENABLED=1 \
  -e ANAMNESIS_SCHEDULER_ENABLED=1 \
  -e DIGEST_SCHEDULER_ENABLED=1 \
  -e SYSTEM_CHECK_ENABLED=1 \
  -e GATE_WATCH_ENABLED=1 \
  -e SMALL_ALERTS_ENABLED=1 \
  -e HEALTH_WATCHDOG_ENABLED=1 \
  -e NUTRITION_REPORTS_ENABLED=1 \
  -e WEEKLY_ADVISOR_ENABLED=1 \
  -e ANOMALY_DETECTOR_ENABLED=1 \
  -e MEDS_FROM_CALENDAR_ENABLED=1 \
  -e MONTHLY_TREND_ENABLED=1 \
  -e ISSUE_REVIEW_ENABLED=1 \
  -e RESEARCH_SCAN_ENABLED=1 \
  -e FOOD_DIARY_BOT_ENABLED=1 \
  -e FOOD_DIARY_BOT_TOKEN="$FOOD_DIARY_BOT_TOKEN" \
  -e CARD_PROCESSOR_ENABLED=1 \
  -e PHENOAGE_CALC_ENABLED=1 \
  -e YANDEX_CLIMATE_ENABLED=1 \
  -e HOST_METRICS_ENABLED=1 \
  -e RECOMMENDATIONS_EVAL_ENABLED=1 \
  -e CONSILIUM_SCHEDULER_ENABLED=1 \
  -e PROBLEM_MAINTENANCE_ENABLED=1 \
  -e YANDEX_IOT_TOKEN="$YANDEX_IOT_TOKEN" \
  -e CARD_GOOGLE_CLIENT_ID="$CARD_GOOGLE_CLIENT_ID" \
  -e CARD_GOOGLE_CLIENT_SECRET="$CARD_GOOGLE_CLIENT_SECRET" \
  -e CARD_GOOGLE_SHEETS_REFRESH_TOKEN="$CARD_GOOGLE_SHEETS_REFRESH_TOKEN" \
  -e CARD_GOOGLE_CALENDAR_REFRESH_TOKEN="$CARD_GOOGLE_CALENDAR_REFRESH_TOKEN" \
  -e RESCUETIME_API_KEY="$RESCUETIME_API_KEY" \
  -e REGISTRAR_MODEL="${REGISTRAR_MODEL:-z-ai/glm-5.3-flash}" \
  -e REGISTRAR_PDF_MODEL="${REGISTRAR_PDF_MODEL:-google/gemini-3.1-flash-lite}" \
  -e REGISTRAR_HEALTH_SCHEMA="${REGISTRAR_HEALTH_SCHEMA:-health}" \
  -e DASHBOARD_TOKEN="$DASHBOARD_TOKEN" \
  -e WIDGET_TOKEN="$WIDGET_TOKEN" \
  -e ERR_DEDUP_TOKEN="$ERR_DEDUP_TOKEN" \
  -e NUTRITION_BOT_TOKEN="$NUTRITION_BOT_TOKEN" \
  -e ACTION_ACK_TOKEN="$ACTION_ACK_TOKEN" \
  -e BACKUP_STATUS_TOKEN="$BACKUP_STATUS_TOKEN" \
  card-service:latest

echo "card-service started. Проверка: curl http://127.0.0.1:8080/health"
