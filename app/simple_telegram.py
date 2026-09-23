"""Минимальная синхронная обёртка sendMessage для ботов, которым не нужен
весь набор app/doctor/telegram.py (typing/edit/file-download — это про
диалог с пациентом). Используется app/hermes_telegram.py и
app/nutrition_telegram.py — каждый просто передаёт свой токен."""
import httpx

from app.telegram_safe import raise_for_status_safe

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(token: str, chat_id: str, text: str, parse_mode: str | None = None,
                  timeout: float = 10.0) -> None:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = httpx.post(_API_BASE.format(token=token), json=payload, timeout=timeout)
    raise_for_status_safe(resp)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API sendMessage failed: {data}")
