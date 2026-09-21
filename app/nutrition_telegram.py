"""Отчёты по питанию — отдельный бот @vvk_gemini_bot (n8n-эра: credential
"Отчет по питанию", id efFiNDygGmGLdiyM), НЕ бот доктора и не Hermes.

2026-09-21 (по прямому запросу Влада): дневной и недельный отчёты о питании
(app/nutrition_reports.py) при переносе с n8n стали уходить через
TELEGRAM_BOT_TOKEN (бот доктора) — в n8n оба воркфлоу ("Reports" и
"Weekly Food Report") слали через отдельный кред "Отчет по питанию",
проверено по backups/wf_snapshots/reports_pre_deactivate_20260919.json и
weekly_food_report_pre_deactivate_20260919.json. Восстановлено сюда."""
import os

from app import simple_telegram

CHAT_ID = "8956401"


def _token() -> str:
    token = os.environ.get("NUTRITION_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("NUTRITION_BOT_TOKEN не задан")
    return token


def send_message(chat_id: str, text: str, parse_mode: str | None = None) -> None:
    simple_telegram.send_message(_token(), chat_id, text, parse_mode)
