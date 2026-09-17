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
  -e REGISTRAR_MODEL="${REGISTRAR_MODEL:-z-ai/glm-5.3-flash}" \
  -e REGISTRAR_PDF_MODEL="${REGISTRAR_PDF_MODEL:-google/gemini-3.1-flash-lite}" \
  -e REGISTRAR_HEALTH_SCHEMA="${REGISTRAR_HEALTH_SCHEMA:-health}" \
  -e DASHBOARD_TOKEN="$DASHBOARD_TOKEN" \
  -e CAPITAN_RELAY_URL="${CAPITAN_RELAY_URL:-http://n8n:443/webhook/doctor-relay-c8f3a9}" \
  card-service:latest

echo "card-service started. Проверка: curl http://127.0.0.1:8080/health"
