#!/bin/bash
# card-service — Phase 0. Публично не торчит: порт только на 127.0.0.1, сеть pgnet
# (та же, что у pg и n8n — см. backups/infra/pg_run.sh в основном репозитории).
# Волна 1 (A3, 2026-09-17): TELEGRAM_POLLING_ENABLED и CAPITAN_RELAY_URL закреплены
# Хотфикс 17.09 (ZCode): + DASHBOARD_TOKEN — без него /dashboard/* отдаёт 403 fail-closed
# Волна 2 (B1): ANAMNESIS_SCHEDULER_ENABLED — анамнез-планировщик (ежедневно 11:00 VL)
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
# 2026-09-20: DIET_TAGGER_ENABLED — app/diet_tagger.py (каждые 15 мин), порт
# n8n Diet Quality Tagger. Первый порт группы 2 с реальным LLM-вызовом.
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
# 2026-09-20: ANOMALY_DETECTOR_ENABLED + CARD_GOOGLE_*/RESCUETIME_API_KEY —
# группа 3 (1/2, вместе с Collect_Biohacking_Data -> POST /ingest/biohacking,
# см. app/biohacking_ingest.py). CARD_GOOGLE_CLIENT_ID/SECRET общие для
# Sheets и Calendar (один OAuth-клиент); refresh_token у каждого свой —
# Calendar-креды отдельно авторизованы Владом на перенос (более
# чувствительные данные, чем ячейки таблицы). RESCUETIME_API_KEY раньше
# лежал открытым текстом в параметрах n8n-ноды — вынесен в переменную
# окружения (гигиена, не поведенческое отличие).
set -e
cd "$(dirname "$0")"

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
  -e SYSTEM_CHECK_ENABLED=1 \
  -e GATE_WATCH_ENABLED=1 \
  -e SMALL_ALERTS_ENABLED=1 \
  -e DIET_TAGGER_ENABLED=1 \
  -e HEALTH_WATCHDOG_ENABLED=1 \
  -e NUTRITION_REPORTS_ENABLED=1 \
  -e WEEKLY_ADVISOR_ENABLED=1 \
  -e ANOMALY_DETECTOR_ENABLED=1 \
  -e MEDS_FROM_CALENDAR_ENABLED=1 \
  -e MONTHLY_TREND_ENABLED=1 \
  -e CARD_GOOGLE_CLIENT_ID="$CARD_GOOGLE_CLIENT_ID" \
  -e CARD_GOOGLE_CLIENT_SECRET="$CARD_GOOGLE_CLIENT_SECRET" \
  -e CARD_GOOGLE_SHEETS_REFRESH_TOKEN="$CARD_GOOGLE_SHEETS_REFRESH_TOKEN" \
  -e CARD_GOOGLE_CALENDAR_REFRESH_TOKEN="$CARD_GOOGLE_CALENDAR_REFRESH_TOKEN" \
  -e RESCUETIME_API_KEY="$RESCUETIME_API_KEY" \
  -e REGISTRAR_MODEL="${REGISTRAR_MODEL:-z-ai/glm-5.3-flash}" \
  -e REGISTRAR_PDF_MODEL="${REGISTRAR_PDF_MODEL:-google/gemini-3.1-flash-lite}" \
  -e REGISTRAR_HEALTH_SCHEMA="${REGISTRAR_HEALTH_SCHEMA:-health}" \
  -e DASHBOARD_TOKEN="$DASHBOARD_TOKEN" \
  -e CAPITAN_RELAY_URL="${CAPITAN_RELAY_URL:-http://n8n:443/webhook/doctor-relay-c8f3a9}" \
  card-service:latest

echo "card-service started. Проверка: curl http://127.0.0.1:8080/health"
