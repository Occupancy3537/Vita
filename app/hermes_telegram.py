"""Системные алерты — отдельный бот @Hermes_AI_vvk_bot (n8n-эра: credential
"Hermes Agent", id waRQHViWZ0reo1qC), НЕ бот доктора.

2026-09-21 (по прямому запросу Влада): при переносе с n8n на card-service все
алерт-воркфлоу (_Error Handler, _Err Dedup, _System Check, Anomaly_Detector,
_Backup Alert, _Memory Pre-Archive Check, Weekly AI Advisor,
Monthly_Trend_Wellness) стали слать через один-единственный TELEGRAM_BOT_TOKEN
(бот доктора, @AI_VVK_Doctor_bot) — просто потому что на тот момент в
card-service не было другого способа отправить сообщение. Это расхождение с
оригиналом: в n8n все перечисленные воркфлоу использовали Hermes Agent,
проверено по снапшотам (backups/wf_snapshots/*, backups/wf_error_handler.json,
backups/infra/mk_errdedup.js) — только Health_Watchdog в n8n-версии
действительно слал через бота доктора (health_watchdog.py оставлен как есть).
Восстановлено сюда."""
import os

from app import simple_telegram

CHAT_ID = "8956401"


def _token() -> str:
    token = os.environ.get("HERMES_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("HERMES_BOT_TOKEN не задан")
    return token


def send_message(chat_id: str, text: str, parse_mode: str | None = None) -> None:
    simple_telegram.send_message(_token(), chat_id, text, parse_mode)
