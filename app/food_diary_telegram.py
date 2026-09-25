"""Дневник питания — @vlad_health_2026_bot (сам бот и его long-polling цикл
живут в app/food_diary_bot.py; этот модуль — минимальный push-отправитель для
случаев, когда нужно отправить сообщение ВНЕ обработки входящего апдейта,
тот же паттерн, что app/service_telegram.py — общая обёртка simple_telegram.py).

2026-09-24 (тикет «раскладка ботов по тематическим чатам»): итог дня по питанию
(app/nutrition_reports.py::run_daily()) переехал сюда из общего вечернего
дайджеста — тематически он про еду, тот же чат, где Влад и так ведёт дневник."""
import os

from app import simple_telegram

CHAT_ID = "8956401"


def _token() -> str:
    token = os.environ.get("FOOD_DIARY_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("FOOD_DIARY_BOT_TOKEN не задан")
    return token


def send_message(chat_id: str, text: str, parse_mode: str | None = None) -> None:
    simple_telegram.send_message(_token(), chat_id, text, parse_mode)
