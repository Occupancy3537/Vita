#!/usr/bin/env bash
# Публикация цен лабораторий: seed card.lab_item из latest.json.
# Запускается ВРУЧНУЮ после приёмки (Claude/Влад) — скрипт сбора БД не пишет.
#
# Требует env БД card-service (CARD_PG_HOST/USER/PASSWORD/...): запускать с
# окружением контейнера (см. RUNBOOK, раздел «Сбор цен лабораторий»).
set -euo pipefail

CARD=/home/openclaw/longevity-project/card-service
LATEST=/home/openclaw/lab_prices/latest.json

[ -f "$LATEST" ] || { echo "нет $LATEST — сначала успешный (прошедший ворота) запуск сбора"; exit 1; }
cd "$CARD"
source .venv/bin/activate

python - <<'PY'
import os, sys
need = ["CARD_PG_HOST", "CARD_PG_USER", "CARD_PG_PASSWORD"]
missing = [v for v in need if not os.environ.get(v)]
if missing:
    sys.exit("нет env БД (запустите с окружением card-service): " + ", ".join(missing))
print("env БД на месте; схема:", os.environ.get("CARD_PG_SCHEMA", "card"))
PY

echo "=== seed из $LATEST (upsert, идемпотентно) ==="
python -m app.lab_prices_ingest seed "$LATEST"
echo "=== отчёт: незмапленные + ключи маппинга без позиции ==="
python -m app.lab_prices_ingest report
