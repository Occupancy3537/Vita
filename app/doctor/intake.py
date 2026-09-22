"""
Telegram update -> IncomingMessage (план §3.2, §3.9). `parse_update` — чистая
функция, без сети и БД: тот же сырой dict Телеграма приходит сегодня через
временный HTTP-хоп из n8n, завтра — через long-polling/собственный вебхук, разбор
не меняется.

`handle_update` — транспорт-агностичный обработчик одного хода целиком (§3.2:
"handle_update(update) -> None"). Гейт красных флагов (gate.py, Phase 2) —
L3 короткое замыкание, ответ без модели. Всё остальное идёт в агентный цикл
(loop.py, Phase 4) — реальный разбор, не заглушка.
"""
import logging
import re
import time
from typing import Optional

from psycopg.errors import UniqueViolation

from app import hermes_telegram
from app.db import get_conn
from app.doctor import commit, gate, loop, render, telegram
from app.doctor.commit import CommitError
from app.doctor.contract import IncomingMessage
from app.doctor.dialog import already_processed, write_turn

logger = logging.getLogger(__name__)

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


# F1 (внешний аудит логики, 2026-09-22): эмердженси-ответ уходил одним вызовом
# Telegram — сбой отправки (сеть/429/бот заблокирован) означал, что пациент
# НИКОГДА не увидит «вызовите скорую», а слою B (slow_gate_followup) вообще не
# давали шанса запуститься. Теперь: 3 попытки ботом доктора с паузой, затем
# фолбэк через Hermes-бот — независимая доставка в тот же чат (chat_id личного
# чата совпадает для всех ботов, оба принадлежат Владу).
EMERGENCY_SEND_ATTEMPTS = 3
EMERGENCY_RETRY_DELAY_SECONDS = 1.5


def _deliver_emergency(msg: IncomingMessage, reply_text: str) -> bool:
    """Доставка эмердженси-ответа с ретраями и фолбэком. Никогда не бросает:
    сбой доставки не должен ронять обработку — эпизод и rf_event к этому
    моменту уже записаны в карту (gate.handle_emergency), теряется только
    уведомление пациенту, и об этом громко пишем в лог."""
    for attempt in range(1, EMERGENCY_SEND_ATTEMPTS + 1):
        try:
            telegram.send_message(msg.chat_id, reply_text, reply_to_message_id=msg.message_id)
            return True
        except Exception:
            logger.exception("intake: попытка %d/%d доставить эмердженси ботом доктора не удалась",
                             attempt, EMERGENCY_SEND_ATTEMPTS)
            if attempt < EMERGENCY_SEND_ATTEMPTS:
                time.sleep(EMERGENCY_RETRY_DELAY_SECONDS * attempt)
    try:
        hermes_telegram.send_message(msg.chat_id, reply_text)
        logger.warning("intake: эмердженси доставлен фолбэком через Hermes-бот "
                       "(бот доктора не смог; update=%s, chat=%s)", msg.update_id, msg.chat_id)
        return True
    except Exception:
        logger.critical("intake: эмердженси НЕ доставлен ни одним ботом (update=%s, chat=%s) — "
                        "эпизод в карте записан, пациент не уведомлён",
                        msg.update_id, msg.chat_id)
        return False


def handle_update(update: dict) -> None:
    msg = parse_update(update)
    if msg is None:
        return

    text = _fallback_text(msg)
    emergency_reply: Optional[str] = None
    user_turn_id: Optional[str] = None

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if already_processed(cur, msg.chat_id, msg.update_id):
                    return
                user_turn_id = write_turn(
                    cur, chat_id=msg.chat_id, update_id=msg.update_id,
                    role="user", text=text,
                    meta={"kind": msg.kind, "reply_symptom_id": msg.reply_symptom_id},
                )

                # Гейт красных флагов — ДО модели, в той же транзакции, что и
                # user-ход (план §3.1). fast_gate — только A+bracelet, без сети,
                # <200мс; единственное решение, которое отсюда может выйти — L3.
                gate_result = gate.fast_gate(cur, text)
                if gate_result["result"].get("level") == "L3":
                    emergency_reply = gate.handle_emergency(cur, msg.chat_id, gate_result, text, None)
            conn.commit()
    except UniqueViolation:
        return  # тот же update_id уже вставлен параллельным вызовом — гонка, не баг

    if emergency_reply is not None:
        # Короткое замыкание: модель не вызывается вообще. Слой B всё равно
        # считается — ПОСЛЕ ответа, дописывает ту же сессию, если у него
        # найдётся что добавить (никогда не задерживает эмердженси, §3.7).
        # F1: доставка — с ретраями и фолбэком через Hermes, см. _deliver_emergency.
        _deliver_emergency(msg, emergency_reply)
        gate.slow_gate_followup(text)
        return

    telegram.send_chat_action(msg.chat_id, "typing")
    placeholder_id = telegram.send_message(msg.chat_id, "…", reply_to_message_id=msg.message_id)

    result = loop.run_turn(chat_id=msg.chat_id, person_id=msg.person_id, text=text, turn_id=user_turn_id)
    reply_text = render.sanitize_for_telegram(result.reply_text)

    # Коммит — ДО ответа пациенту (быстрый, только локальные транзакции, без
    # сети): если инвариант нарушен (план §3.6: второе открытое расследование
    # и т.п.), это отказ, а не тихая потеря — узнаём до, а не после того, как
    # уже сказали пациенту "записал".
    commit_error: Optional[str] = None
    commit_result = {"committed": False, "reason": "nothing_to_write"}
    if result.staged_writes:
        try:
            commit_result = commit.apply_staged_writes(result.staged_writes, turn_id=user_turn_id)
        except CommitError as e:
            commit_error = str(e)

    telegram.edit_message(msg.chat_id, placeholder_id, reply_text, parse_mode="HTML")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Отложенные записи — в meta целиком, вместе с исходом коммита: даже
            # если инвариант отклонил запись, сам факт попытки и содержимое не
            # теряются молча (тихая потеря данных — риск №1 проекта, CLAUDE.md).
            write_turn(cur, chat_id=msg.chat_id, update_id=None, role="assistant",
                       text=reply_text, wrote_anything=commit_result.get("committed", False),
                       meta={"staged_writes": [w.model_dump() for w in result.staged_writes],
                             "commit_result": commit_result, "commit_error": commit_error}
                       if result.staged_writes else None)
        conn.commit()

    # L1/L2 (только слой B их порождает — см. gate.py) детектируются здесь же,
    # уже после ответа: пишут rf_event, но пока не меняют сам ответ — вставка
    # строки "показаться врачу сегодня" для L2 требует знать уровень ДО ответа
    # модели, а B считается параллельно ей же — честный нерешённый разрыв,
    # не забытый: пока L2 виден только в rf_event, не в тексте пациенту.
    gate.slow_gate_followup(text)
