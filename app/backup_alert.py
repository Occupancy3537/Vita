"""Порт n8n `_Backup Alert` (2026-09-20, группа малых утилит) — следит за
ночным бэкапом (`backups/infra/nightly_backup.sh`).

Два входа, как в оригинале:
- webhook (POST): сам `nightly_backup.sh` пингует после каждого прогона с
  result/detail/ts — сохраняем состояние, шумим сразу при failed/partial.
- расписание (раз в сутки): если пинга не было >26ч — бэкап вообще не
  отработал (крон умер, скрипт упал раньше curl) — самостоятельный сигнал,
  не зависящий от того, дошёл ли когда-нибудь webhook.

Состояние — в Postgres (health.backup_alert_state, 1 строка), не в памяти
процесса: переживает рестарт card-service, как и $getWorkflowStaticData
переживал рестарт n8n."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from app.db import get_conn
from app import hermes_telegram as telegram  # 2026-09-21: алерты -> Hermes, не бот доктора (см. app/hermes_telegram.py)
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
# Оригинальный n8n-воркфлоу был на workflow-level timezone: UTC (проверено
# settings.timezone, а не по названию ноды "Daily 07:00 UTC" — это название
# разошлось с реальным triggerAtHour: 9, оставлено n8n как есть, не мой баг,
# просто беру настоящее значение параметра, не текст лейбла).
CHECK_HOUR_UTC = 9
# 2026-09-22 (внешний аудит, K3 — КРИТИЧНО): значение раньше было литералом
# здесь И в backups/infra/nightly_backup.sh — вынесено в env
# (BACKUP_STATUS_TOKEN, run.sh) и ротировано (засветилось внешнему аудиту).
WEBHOOK_TOKEN = os.environ.get("BACKUP_STATUS_TOKEN", "")
STALE_AFTER_HOURS = 26


def _load_state(cur) -> dict | None:
    cur.execute("SELECT ts, result, detail, stamp FROM health.backup_alert_state WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return None
    ts, result, detail, stamp = row
    return {"ts": ts, "result": result, "detail": detail or "", "stamp": stamp or ""}


def _store_state(cur, result: str, detail: str, stamp: str) -> None:
    cur.execute(
        "INSERT INTO health.backup_alert_state (id, ts, result, detail, stamp) VALUES (1, now(), %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET ts = now(), result = EXCLUDED.result, "
        "detail = EXCLUDED.detail, stamp = EXCLUDED.stamp",
        (result, detail, stamp),
    )


def handle_ping(token: str, result: str, detail: str, stamp: str) -> str:
    """Порт ветки src === 'webhook' из Assess. Возвращает текст алерта ('' —
    без алерта). Fail-closed на токен, как и в оригинале — пустой
    WEBHOOK_TOKEN (переменная не задана) тоже отказ, не "токен не нужен"."""
    if not WEBHOOK_TOKEN or token != WEBHOOK_TOKEN:
        return ""
    with get_conn() as conn, conn.cursor() as cur:
        _store_state(cur, result, detail, stamp)
        conn.commit()

    if result == "failed":
        return f"⚠️ <b>Ночной бэкап: СБОЙ</b>\n\nЭтап(ы): {detail or 'неизвестно'}\nВремя: {stamp} UTC\n\nЛог: nightly_backup.log на VPS."
    if result == "partial":
        return f"ℹ️ <b>Бэкап: один провайдер не сработал</b>\n\n{detail} · {stamp} UTC.\nБэкап цел на остальных облаках. Проверь авторизацию rclone, если повторится."
    if result != "ok":
        return f"⚠️ Бэкап вернул статус «{result}» — {detail}"
    return ""


def check_stale() -> str:
    """Порт ветки src === 'schedule' — не с чем сверяться в вебхуке, отдельный
    ежедневный сигнал "пинга вообще не было"."""
    with get_conn() as conn, conn.cursor() as cur:
        state = _load_state(cur)
    if state is None or state["ts"] is None:
        age_h = 999.0
        stamp = "никогда"
    else:
        age_h = (datetime.now(timezone.utc) - state["ts"]).total_seconds() / 3600
        stamp = state["stamp"] or "?"

    if age_h > STALE_AFTER_HOURS:
        return f"⚠️ <b>Ночной бэкап не отработал</b>\n\nПоследний пинг: {stamp} ({round(age_h)}ч назад).\nПроверь крон и nightly_backup.log."
    if state and state["result"] == "failed":
        return f"⚠️ <b>Ночной бэкап: последний прогон со сбоем</b>\n\n{state['detail']} · {state['stamp']}"
    return ""


def run_once() -> None:
    alert = check_stale()
    if alert:
        telegram.send_message(CHAT_ID, alert, parse_mode="HTML")


def run_scheduler() -> None:
    logger.info("backup_alert scheduler: старт")
    while True:
        try:
            now = datetime.now(timezone.utc)
            nxt = now.replace(hour=CHECK_HOUR_UTC, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            run_once()
        except Exception as e:
            logger.exception("backup_alert run_once упал — повтор завтра")
            alert_on_failure("backup_alert", e)
            time.sleep(3600)
