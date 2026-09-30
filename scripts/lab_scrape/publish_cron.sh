#!/usr/bin/env bash
# Еженедельная публикация цен лабораторий: сбор -> ворота -> seed --prune.
# Вызывается cron'ом (сервер по Berlin); PATH/venv/env задаются здесь, не в crontab.
set -uo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CARD=/home/openclaw/longevity-project/card-service
BASE=/home/openclaw/lab_prices
LOG="$BASE/scrape.log"

{
  echo "=== cron-запуск $(date -u +%FT%TZ) ==="
  cd "$CARD"
  # env БД card-service (json-экспорт из контейнера, без системных переменных)
  sudo docker inspect card-service --format '{{json .Config.Env}}' > /tmp/env_raw_cron.json
  python3 - <<'PY'
import json, shlex
env = json.load(open("/tmp/env_raw_cron.json"))
with open("/tmp/lab_env_cron.sh", "w") as f:
    f.write("# cron env (600)\n")
    for item in env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        if k in ("PATH", "HOME", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHON_VERSION", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256"):
            continue
        f.write(f"export {k}={shlex.quote(v)}\n")
PY
  chmod 600 /tmp/lab_env_cron.sh
  set -a; source /tmp/lab_env_cron.sh; set +a
  export CARD_PG_HOST=127.0.0.1
  unset CARD_PG_SCHEMA

  # 1) сбор (ворота качества внутри; провал = код 3, latest.json не меняется)
  /home/openclaw/lab-scraper/.venv/bin/python -m scripts.lab_scrape run --base "$BASE"

  # 2) публикация: seed --prune только если latest.json прошёл ворота
  if [ -f "$BASE/latest.json" ] && [ -z "$(find "$BASE/latest.json" -mtime +8 2>/dev/null)" ]; then
    $CARD/.venv/bin/python -m app.lab_prices_ingest seed --prune "$BASE/latest.json"
  else
    echo "cron: latest.json отсутствует или старше 8 дней — публикация отменена"
  fi
  rm -f /tmp/env_raw_cron.json /tmp/lab_env_cron.sh
} >> "$LOG" 2>&1
