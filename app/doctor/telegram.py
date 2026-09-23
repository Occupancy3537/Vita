"""
Telegram Bot API — тонкая синхронная обёртка (httpx, тот же паттерн, что уже есть
в app/extraction.py и app/redflag_b.py: весь card-service синхронный, объём
трафика — единицы сообщений в день, П5-спека §10 — asyncio не оправдан, см.
app/db.py). Один бот на весь проект (`AI_VVK_Doctor_bot`).

2026-09-23 (живой инцидент): старый TELEGRAM_BOT_TOKEN был скомпрометирован
ещё в n8n-эру (см. backups/infra/SECRETS.md, "утёк в git plaintext") — ротация
была отложена месяцами как "не блокирует Phase 1", пока кто-то реально не
воспользовался токеном (свой вебхук на чужой домен, поллер доктора сутки не
получал апдейты). Ротирован через BotFather тем же днём. Заодно —
raise_for_status_safe() (app/telegram_safe.py) вместо голого raise_for_status():
исключение httpx на ошибке несёт URL с токеном ЦЕЛИКОМ в своём тексте, и именно
так этот инцидент впервые бросился в глаза — в docker logs.

parse_mode не проставляется автоматически внутри этой обёртки — вызывающий
(intake.py) сам прогоняет текст через render.sanitize_for_telegram() и передаёт
parse_mode="HTML" явно. Плейсхолдер ("…") и эмердженси-ответ (без тегов)
уходят без parse_mode — для них это не нужно, а не пропущено по невнимательности."""
import os
from typing import Optional

import httpx

from app.telegram_safe import raise_for_status_safe

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_FILE_BASE = "https://api.telegram.org/file/bot{token}/{file_path}"


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан — см. backups/infra/SECRETS.md")
    return token


def _call(method: str, payload: dict, timeout: float = 10.0) -> dict:
    resp = httpx.post(_API_BASE.format(token=_token(), method=method), json=payload, timeout=timeout)
    raise_for_status_safe(resp)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    return data["result"]


def send_chat_action(chat_id: str, action: str = "typing") -> None:
    """Индикатор "печатает" — не критичный путь (Телеграм и так покажет ответ),
    сбой здесь не должен срывать сам ответ."""
    try:
        _call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5.0)
    except Exception:
        pass


def send_message(chat_id: str, text: str, reply_to_message_id: Optional[int] = None,
                  parse_mode: Optional[str] = None, force_reply: bool = False) -> int:
    """Возвращает message_id — нужен для editMessageText (плейсхолдер -> финальный
    ответ, план §3.2: edit вместо send+delete). force_reply — анамнез-вопросы
    (Волна 2/B1): Telegram сам открывает поле ответа."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if force_reply:
        payload["reply_markup"] = {"force_reply": True, "selective": False}
    result = _call("sendMessage", payload)
    return result["message_id"]


def edit_message(chat_id: str, message_id: int, text: str, parse_mode: Optional[str] = None) -> None:
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    _call("editMessageText", payload)


def get_file_path(file_id: str) -> str:
    result = _call("getFile", {"file_id": file_id})
    return result["file_path"]


def download_file(file_id: str, timeout: float = 20.0) -> bytes:
    file_path = get_file_path(file_id)
    resp = httpx.get(_FILE_BASE.format(token=_token(), file_path=file_path), timeout=timeout)
    raise_for_status_safe(resp)
    return resp.content
