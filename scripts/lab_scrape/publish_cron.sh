#!/usr/bin/env bash
# Еженедельная публикация цен лабораторий: сбор -> ворота качества -> seed --prune.
# Вызывается cron'ом (сервер по Berlin, TZ= в cron не поддерживается); PATH/venv/env задаёт сама.
# Принцип наименьших прав: из env контейнера берутся ТОЛЬКО CARD_PG_*, конвейером (полный env
# на диск не пишется); файл реквизитов — 600 в каталоге 700, удаляется при любом выходе (trap).
set -uo pipefail
umask 077

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CARD=/home/openclaw/longevity-project/card-service
BASE=/home/openclaw/lab_prices
LOG="$BASE/scrape.log"
ENV_FILE="$BASE/.env_db"

cleanup() { rm -f "$ENV_FILE"; }
trap cleanup EXIT

{
  echo "=== cron-запуск $(date -u +%FT%TZ) ==="
  cd "$CARD" || { echo "нет каталога $CARD"; exit 2; }

  if ! sudo docker inspect card-service --format '{{json .Config.Env}}' | python3 -c '
import json, shlex, sys
keep = ("CARD_PG_HOST", "CARD_PG_USER", "CARD_PG_PASSWORD", "CARD_PG_DATABASE", "CARD_PG_PORT")
env = json.load(sys.stdin)
lines = []
for item in env:
    k, _, v = item.partition("=")
    if k in keep:
        lines.append("export %s=%s" % (k, shlex.quote(v)))
if not any(l.startswith("export CARD_PG_PASSWORD=") for l in lines):
    sys.exit(3)
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
' "$ENV_FILE"; then
    echo "не удалось получить реквизиты БД из контейнера card-service — цикл отменён"
    exit 2
  fi

  set -a; source "$ENV_FILE"; set +a
  export CARD_PG_HOST=127.0.0.1

  # 1) сбор (ворота качества внутри; провал = ненулевой код, latest.json не меняется)
  if /home/openclaw/lab-scraper/.venv/bin/python -m scripts.lab_scrape run --base "$BASE"; then
    # 2) публикация только после успешного сбора этого запуска
    "$CARD/.venv/bin/python" -m app.lab_prices_ingest seed --prune "$BASE/latest.json"
  else
    echo "сбор не прошёл ворота качества или упал (код $?) — публикация НЕ выполняется, в базе прежние цены"
  fi
  echo "=== cron-запуск завершён ==="
} >> "$LOG" 2>&1
