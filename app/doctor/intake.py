"""
Telegram update -> IncomingMessage (план §3.2, §3.9). `parse_update` — чистая
функция, без сети и БД: тот же сырой dict Телеграма приходит сегодня через
временный HTTP-хоп из n8n, завтра — через long-polling/собственный вебхук, разбор
не меняется.

`handle_update` — транспорт-агностичный обработчик одного хода целиком (§3.2:
"handle_update(update) -> None"). Сейчас (Phase 1 плана) это временная заглушка:
приём, идемпотентность, диалоговая память и настоящий Telegram round-trip уже
на месте, а разбор жалобы — фиксированный текст. gate.py (Phase 2) и loop.py
(Phase 4) подключаются СЮДА, в этот же handle_update, не рядом отдельным путём.
"""
import re
from typing import Optional

from psycopg.errors import UniqueViolation

from app.db import get_conn
from app.doctor import telegram
from app.doctor.contract import IncomingMessage
from app.doctor.dialog import already_processed, write_turn

_SYM_TAG_RE = re.compile(r"#SYM:([A-Za-z0-9_-]+)")


def _extract_sym_tag(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = _SYM_TAG_RE.search(text)
    return m.group(1) if m else None


def parse_update(update: dict) -> Optional[IncomingMessage]:
    """None — апдейт не несёт сообщения, на которое есть смысл отвечать (например,
    callback_query или my_chat_member). update_id всё равно принадлежит Телеграму
    целиком, а не только текстовым сообщениям — если понадобится реагировать на
    другие типы, это отдельное расширение, не патч этой функции "на всякий случай"."""
    update_id = update.get("update_id")
    msg = update.get("message") or update.get("edited_message")
    if update_id is None or msg is None:
        return None

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text") or msg.get("caption")

    kind = "text"
    photo_file_ids: list[str] = []
    voice_file_id = None
    document_file_id = None

    if msg.get("photo"):
        kind = "photo"
        photo_file_ids = [p["file_id"] for p in msg["photo"]]  # Telegram: по возрастанию размера
    elif msg.get("voice"):
        kind = "voice"
        voice_file_id = msg["voice"]["file_id"]
    elif msg.get("document"):
        kind = "document"
        document_file_id = msg["document"]["file_id"]
    elif text is None:
        kind = "unknown"

    reply = msg.get("reply_to_message")
    reply_to_message_id = reply.get("message_id") if reply else None
    reply_to_text = reply.get("text") if reply else None

    forward_from = None
    if msg.get("forward_from"):
        forward_from = msg["forward_from"].get("username") or msg["forward_from"].get("first_name")
    elif msg.get("forward_from_chat"):
        forward_from = msg["forward_from_chat"].get("title")

    return IncomingMessage(
        chat_id=chat_id,
        update_id=update_id,
        message_id=msg.get("message_id"),
        text=text,
        kind=kind,
        photo_file_ids=photo_file_ids,
        voice_file_id=voice_file_id,
        document_file_id=document_file_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
        reply_symptom_id=_extract_sym_tag(reply_to_text),
        forward_from=forward_from,
        raw_update=update,
    )


def _fallback_text(msg: IncomingMessage) -> str:
    return msg.text or f"[{msg.kind}]"


def handle_update(update: dict) -> None:
    msg = parse_update(update)
    if msg is None:
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if already_processed(cur, msg.chat_id, msg.update_id):
                    return
                write_turn(cur, chat_id=msg.chat_id, update_id=msg.update_id,
                           role="user", text=_fallback_text(msg),
                           meta={"kind": msg.kind, "reply_symptom_id": msg.reply_symptom_id})
            conn.commit()
    except UniqueViolation:
        return  # тот же update_id уже вставлен параллельным вызовом — гонка, не баг

    telegram.send_chat_action(msg.chat_id, "typing")
    placeholder_id = telegram.send_message(msg.chat_id, "…", reply_to_message_id=msg.message_id)

    # Phase 1 заглушка — намеренно помечена как таковая, чтобы не читалась как
    # настоящий разбор жалобы. gate.py/loop.py заменят этот блок в Phase 2/4.
    reply_text = (
        "[новый доктор — проверка приёма] Сообщение получено и сохранено "
        f"(«{_fallback_text(msg)[:200]}»). Агентный цикл ещё не подключён — "
        "это только проверка приёма сообщений, диалоговой памяти и ответа в Telegram."
    )

    telegram.edit_message(msg.chat_id, placeholder_id, reply_text)

    with get_conn() as conn:
        with conn.cursor() as cur:
            write_turn(cur, chat_id=msg.chat_id, update_id=None, role="assistant", text=reply_text)
        conn.commit()
