"""Порт n8n `_Err Dedup` (2026-09-21) — последний шаг «можно ли полностью
убрать n8n». Три ночных cron-скрипта (pg_to_sheets_mirror.js,
pg_sheets_diff_check.js, sheets_to_pg_mirror.js) запускаются НАПРЯМУЮ через
`/usr/bin/node` на хосте (НЕ через n8n — проверено crontab'ом) и не зависят
от n8n ни для чего, КРОМЕ одного: при сбое каждый из них стучался в
n8n-вебхук `/webhook/err-dedup` для дедуплицированного алерта в Telegram —
единственная оставшаяся причина, по которой n8n ещё нельзя было выключить
полностью. Порт — эндпоинт `/err-dedup` здесь же в card-service, скрипты
просто меняют port 5678 -> 8080 и path /webhook/err-dedup -> /err-dedup, ни
токен (8beNA4tqEdqhpjtUqCUM), ни тело запроса не меняются.

Дедуп-состояние (было $getWorkflowStaticData('global').seen{}, персистентно
между production-прогонами активного n8n-воркфлоу) — здесь в
card.err_dedup_state (та же природа состояния, что и telegram_poll_state в
той же схеме). Логика 1:1: подавляем повтор той же пары (wf, node) в
пределах 60 минут; если сбои продолжаются пачкой и прошло >=55 минут —
шлём отдельное «серия ошибок продолжается» вместо тишины."""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
EXPECTED_TOKEN = "8beNA4tqEdqhpjtUqCUM"
SUPPRESS_MINUTES = 60
NUDGE_AFTER_MINUTES = 55


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def check_and_notify(cur, wf: str, node: str, telegram_text: str, silent: bool = False, token: str = "") -> dict:
    """Порт "Dedup" (код) — возвращает {send, text, silent, burst}, тот же
    контракт, что оригинальный код-узел. Не отправляет сообщение сама —
    это делает вызывающий (run_notify), чтобы функция оставалась чистой
    и тестируемой без реального Telegram.

    ВАЖНО (сверено построчно с оригиналом): `sd.seen[key] = now` в JS
    выполнялся на КАЖДЫЙ вызов, не только при реальной отправке — окно
    подавления считается от времени ПРЕДЫДУЩЕГО ВЫЗОВА (любого), не от
    времени предыдущей ОТПРАВКИ. Для вызывающих, которые зовут раз в сутки
    (все три ночных cron-скрипта), разница не проявляется — но burst-логика
    рассчитана на частые повторные вызовы (retry/несколько ошибок подряд от
    одного воркфлоу), портирую как есть."""
    if token != EXPECTED_TOKEN:
        return {"send": False, "text": "", "silent": True, "burst": 0}

    key = f"{wf}|{node}"
    now = datetime.now(timezone.utc)

    cur.execute("SELECT last_notified_at, burst_count FROM card.err_dedup_state WHERE key = %s", (key,))
    row = cur.fetchone()
    last_at, burst = (row[0], row[1]) if row else (None, 0)
    mins = (now - last_at).total_seconds() / 60 if last_at else float("inf")
    suppress = mins < SUPPRESS_MINUTES

    new_burst = burst + 1 if suppress else 0

    raw = telegram_text or f"Ошибка: {wf} / {node}"
    send = not suppress

    if suppress and burst > 0 and mins >= NUDGE_AFTER_MINUTES:
        raw = f"🔁 {wf} / {node} — серия ошибок продолжается ({burst} за ~час). Загляни в Error_Log."
        send = True
        new_burst = 0

    # "sd.seen[key] = now" — безусловно на каждый вызов (см. докстринг).
    cur.execute(
        "INSERT INTO card.err_dedup_state (key, last_notified_at, burst_count) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO UPDATE SET last_notified_at = EXCLUDED.last_notified_at, burst_count = EXCLUDED.burst_count",
        (key, now, new_burst),
    )

    return {"send": send, "text": _escape_html(raw), "silent": bool(silent), "burst": new_burst}


def run_notify(cur, wf: str, node: str, telegram_text: str, silent: bool, token: str) -> dict:
    result = check_and_notify(cur, wf, node, telegram_text, silent, token)
    if result["send"]:
        from app.doctor import telegram
        try:
            telegram.send_message(CHAT_ID, result["text"], parse_mode="HTML")
        except Exception:
            logger.exception("err_dedup: не удалось отправить Telegram-алерт (wf=%s node=%s)", wf, node)
    return result
