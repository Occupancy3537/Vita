"""Сервисный бот — @vvk_gemini_bot (n8n-эра: credential "Отчет по питанию",
id efFiNDygGmGLdiyM). Модуль был `nutrition_telegram.py` до 2026-09-24.

2026-09-24 (тикет «раскладка ботов по тематическим чатам»): первые живые сутки
единого вечернего дайджеста показали, что общий поток алертов/дайджеста через
@Hermes_AI_vvk_bot конфликтует — личный ИИ-агент Влада (NousPortal) читал эти
сообщения как команды себе и перехватывал ответы по анамнезу. Решение Влада:
Hermes полностью исключается из проекта; этот бот (уже существующий, раньше
слал только отчёты о питании) становится ЕДИНСТВЕННЫМ транспортом app/notify.py
— все алерты и вечерний дайджест (жёлтые аномалии, weekly/monthly-отчёты,
critical сверх бюджета). Токен (`NUTRITION_BOT_TOKEN`) НЕ переименован —
тот же физический бот, та же переменная окружения, поменялась только роль
(переименовывать секрет в проде ради имени — риск не по бюджету сложности,
см. CLAUDE.md про правку env). Отчёты о питании сами переехали в другой
канал — дневник питания, см. app/food_diary_telegram.py."""
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
