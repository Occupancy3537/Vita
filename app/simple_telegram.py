"""Минимальная синхронная обёртка sendMessage для ботов, которым не нужен
весь набор app/doctor/telegram.py (typing/edit/file-download — это про
диалог с пациентом). Используется app/service_telegram.py и
app/food_diary_telegram.py — каждый просто передаёт свой токен."""
import httpx

from app.telegram_safe import raise_for_status_safe

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# «Тормоз роста» (2026-09-26): до этого лимит Telegram нигде не учитывался —
# длинное сообщение (вечерний дайджест уже был 3280/4096 25.09) просто
# отклонялось API целиком при первом насыщенном дне, хвост терялся молча.
TELEGRAM_MESSAGE_LIMIT = 4096


def _split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет текст на последовательные сообщения по границе лимита. Предпочитает
    резать по пустой строке (двойной перевод — так собраны все многосекционные
    сообщения в проекте, digest.py/weekly_advisor.py — секции разделены "\n\n———\n\n"),
    затем по одиночному переводу строки, и только если сам абзац длиннее лимита —
    жёстко по символам. Хвост никогда не отбрасывается."""
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n\n")
        if split_at <= 0:
            split_at = window.rfind("\n")
        if split_at <= 0:
            split_at = limit  # абзац сам длиннее лимита — жёсткий разрез, не теряем текст
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def send_message(token: str, chat_id: str, text: str, parse_mode: str | None = None,
                  timeout: float = 10.0) -> None:
    for part in _split_text(text):
        payload: dict = {"chat_id": chat_id, "text": part}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = httpx.post(_API_BASE.format(token=token), json=payload, timeout=timeout)
        raise_for_status_safe(resp)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API sendMessage failed: {data}")
