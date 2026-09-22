"""Живой Telegram-бот Food diary_v5 (2026-09-21) — подключение к боту
"vlad_health" (свой токен, FOOD_DIARY_BOT_TOKEN, credential 8CKBKo8CXTaLD3YI
в n8n), СОБСТВЕННЫЙ long-polling цикл, независимый от app.doctor.poller
(тот бот — Hermes Agent, другой токен, другая цель). Решение Влада
2026-09-21: опрос (polling), как у доктора, не вебхук — не плодим новый
публичный маршрут, card-service остаётся слушающим только 127.0.0.1.

Вся чистая логика (классификация сообщений, промпты, парсинг JSON, SQL для
health.meals, статистика) — в app/food_diary.py, полностью протестирована
там. Этот модуль — только проводка: приём апдейтов, вызовы Telegram/
OpenRouter/Sheets API, персистентный offset (card.telegram_poll_state,
id='food_diary' — отдельная строка от доктора, id='singleton').

ФИКС (2026-09-21, по решению Влада «чини»): оригинал никогда не вставлял тег
"[ID:...]" в само подтверждение после записи блюда — только в forceReply-
подсказку кнопки "Изменить последний". Прямой reply на подтверждение (без
нажатия кнопки) тихо терял правку. Здесь confirmation ВСЕГДА содержит
[ID:{entry_id}] — extract_edit_context()/extract_loose_entry_id() находят
его в обоих случаях.

РЕШЕНО остаться как в оригинале (по прямому ответу Влада): порядок
приоритета classify_message() (reply > command > фото+текст > текст > фото)
вместо независимых условий n8n Switch — оставлено как реализовано; дубль-
запись в health.meals в Google Sheets (Nutrition!Meals) — оставлена
навсегда, не только на переходный период.

2026-09-22 (по запросу Влада): call_text_llm/call_photo_llm теперь оба
используют fd.MODEL (единая google/gemini-3.1-flash-lite — GLM 5.3 Flash
из питания убран целиком), и оба места, где раньше стоял голый
fd.parse_json_from_ai(raw), сразу дозаполняют NOVA/veg_g/.../plants через
fd.normalize_food_group_tags() — это то, что раньше делал ОТДЕЛЬНЫЙ,
запускавшийся до 15 минут спустя app/diet_tagger.py (удалён при слиянии,
см. докстринг app/food_diary.py). Побочный эффект: _sync_to_sheet() теперь
дублирует и эти поля в Sheets — раньше туда попадали только нутриенты."""
import base64
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app import food_diary as fd
from app.db import get_conn, schema
from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

VL = timezone(timedelta(hours=10))
_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_FILE_BASE = "https://api.telegram.org/file/bot{token}/{file_path}"
POLL_TIMEOUT = 30
POLL_STATE_ID = "food_diary"
OWNER_CHAT_ID = "8956401"  # 2026-09-22 (внешний аудит, K5): единственный, чьи сообщения обрабатываем

NUTRITION_SHEET_ID = "1NCiBHlbl-nx99kRe8uaqpsAdVV_i6MTw6Bl43LMbCkU"
MEALS_SHEET_TITLE = "Meals"
MEALS_SHEET_GID = 403788598


def _token() -> str:
    token = os.environ.get("FOOD_DIARY_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("FOOD_DIARY_BOT_TOKEN не задан")
    return token


def _call(method: str, payload: dict, timeout: float = 15.0) -> dict:
    resp = httpx.post(_API_BASE.format(token=_token(), method=method), json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    return data["result"]


def send_message(chat_id, text: str, reply_markup: Optional[dict] = None, force_reply: bool = False) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    elif force_reply:
        payload["reply_markup"] = {"force_reply": True}
    return _call("sendMessage", payload)


def edit_message_text(chat_id, message_id, text: str) -> None:
    _call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"})


def answer_callback_query(callback_query_id: str, text: str) -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def get_file_path(file_id: str) -> str:
    return _call("getFile", {"file_id": file_id})["file_path"]


def download_file(file_id: str, timeout: float = 20.0) -> bytes:
    file_path = get_file_path(file_id)
    resp = httpx.get(_FILE_BASE.format(token=_token(), file_path=file_path), timeout=timeout)
    resp.raise_for_status()
    return resp.content


def delete_webhook() -> None:
    try:
        resp = httpx.post(_API_BASE.format(token=_token(), method="deleteWebhook"),
                           json={"drop_pending_updates": False}, timeout=10)
        resp.raise_for_status()
        logger.info("food_diary_bot: webhook deleted: %s", resp.json())
    except Exception:
        logger.exception("food_diary_bot: failed to delete webhook")


def get_updates(offset: int, timeout: float = POLL_TIMEOUT) -> list[dict]:
    resp = httpx.get(
        _API_BASE.format(token=_token(), method="getUpdates"),
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message","callback_query"]'},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data["result"]


def _get_last_offset(cur) -> int:
    cur.execute(f"SELECT last_update_id FROM {schema()}.telegram_poll_state WHERE id = %s", (POLL_STATE_ID,))
    row = cur.fetchone()
    return row[0] if row else 0


def _save_offset(cur, update_id: int) -> None:
    cur.execute(
        f"INSERT INTO {schema()}.telegram_poll_state (id, last_update_id, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET last_update_id = EXCLUDED.last_update_id, updated_at = now()",
        (POLL_STATE_ID, update_id),
    )


# =====================================================================
# LLM-вызовы (текст/фото)
# =====================================================================

def call_text_llm(user_prompt: str, timeout: float = 30.0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return ""
    try:
        resp = httpx.post(
            fd.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": fd.MODEL, "temperature": 0.2, "max_tokens": 3000,
                "provider": {"order": fd.PROVIDER_ORDER, "allow_fallbacks": True},
                "reasoning": {"effort": "low"},
                "messages": [{"role": "system", "content": fd.SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return fd.extract_llm_text(resp.json())
    except Exception:
        logger.exception("food_diary_bot: call_text_llm упал")
        return ""


def call_photo_llm(user_prompt: str, image_bytes: bytes, timeout: float = 30.0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return ""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        resp = httpx.post(
            fd.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": fd.MODEL, "temperature": 0.2, "max_tokens": 3000,
                "provider": {"order": fd.PROVIDER_ORDER, "allow_fallbacks": True},
                "messages": [
                    {"role": "system", "content": fd.SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]},
                ],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return fd.extract_llm_text(resp.json())
    except Exception:
        logger.exception("food_diary_bot: call_photo_llm упал")
        return ""


# =====================================================================
# Двойная запись (Postgres канон + Sheets дубль — оставлен навсегда)
# =====================================================================

def _sync_to_sheet(entry_id: str, user_id: str, date_iso: Optional[str], parsed: dict) -> None:
    from app.sheets_client import append_or_update_row
    row = {**parsed, "Entry_ID": entry_id, "User_ID": user_id}
    if date_iso:
        row["Date"] = date_iso
    try:
        append_or_update_row(NUTRITION_SHEET_ID, MEALS_SHEET_TITLE, "Entry_ID", row)
    except Exception:
        logger.exception("food_diary_bot: не удалось продублировать запись %s в Sheets (Postgres уже записан)", entry_id)


def _delete_from_sheet(entry_id: str) -> None:
    from app.sheets_client import find_row_by_column, delete_row
    try:
        idx = find_row_by_column(NUTRITION_SHEET_ID, MEALS_SHEET_TITLE, "Entry_ID", entry_id)
        if idx is not None:
            delete_row(NUTRITION_SHEET_ID, MEALS_SHEET_GID, idx)
    except Exception:
        logger.exception("food_diary_bot: не удалось удалить запись %s из Sheets (Postgres уже удалён)", entry_id)


def _confirmation_text(parsed: dict, entry_id: str) -> str:
    return (
        f"✅ Записано! 🍽 {parsed.get('Meal_description', '')}\n"
        f"🔥 {parsed.get('Calories', 0)} ккал\n"
        f"🥩 {parsed.get('Proteins', 0)}г белков | {parsed.get('Carbs', 0)}г углеводов | {parsed.get('Fats', 0)}г жиров\n"
        f"Данные сохранены в таблицу.\n[ID:{entry_id}]"
    )


def _confirmation_buttons(entry_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Подтвердить", "callback_data": f"confirm|{entry_id}"},
        {"text": "❌ Удалить", "callback_data": f"delete|{entry_id}"},
        {"text": "⚖️ Изменить последний", "callback_data": f"edit|{entry_id}"},
    ]]}


# =====================================================================
# Обработка callback_query (кнопки)
# =====================================================================

def handle_callback(callback_query: dict) -> None:
    action, row_id = fd.parse_callback_data(callback_query.get("data") or "")
    msg = callback_query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    original_text = msg.get("text") or ""

    answer_callback_query(callback_query["id"], fd.build_answer_text(action))

    if action == "confirm":
        edit_message_text(chat_id, message_id, original_text + "\n\n✅ *Подтверждено*")
    elif action == "delete":
        edit_message_text(chat_id, message_id, original_text + "\n\n❌ *Удалено*")
        if row_id:
            with get_conn() as conn, conn.cursor() as cur:
                fd.delete_meal(cur, row_id)
                conn.commit()
            _delete_from_sheet(row_id)
    elif action == "edit":
        send_message(chat_id, original_text + f"\n\n⚖️ Введите новые данные\n[ID:{row_id}]", force_reply=True)


# =====================================================================
# Обработка message (команды / текст / фото / правка)
# =====================================================================

def _handle_stats(message: dict) -> None:
    user_id = fd.telegram_user_id(message.get("from") or {})
    command = message.get("text") or "/today"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.*, to_char(m.\"Date\" AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD\"T\"HH24:MI') AS \"Date\" "
            'FROM health.meals m ORDER BY m."Date"'
        )
        cols = [c.name for c in cur.description]
        meals = [dict(zip(cols, r)) for r in cur.fetchall()]
    result = fd.build_stats_message(meals, user_id, command)
    send_message(message["chat"]["id"], result["text"], reply_markup=result["reply_markup"])


def _handle_new_entry(message: dict, msg_type: str) -> None:
    user_text = message.get("caption") or message.get("text") or ""
    if msg_type == "text":
        prompt = fd.build_text_prompt(user_text)
        raw = call_text_llm(prompt)
    else:
        photo = message["photo"][-1]  # наибольшее фото, как у доктора (_extract_file_id)
        image_bytes = download_file(photo["file_id"])
        prompt = fd.build_photo_prompt(user_text if msg_type == "text_and_photo" else "")
        raw = call_photo_llm(prompt, image_bytes)

    if not raw:
        send_message(message["chat"]["id"], "⚠️ Не смог распознать блюдо — модель не ответила. Попробуй ещё раз.")
        return
    parsed = fd.parse_json_from_ai(raw)
    parsed.update(fd.normalize_food_group_tags(parsed))

    entry_id = str(message["message_id"])
    user_id = fd.telegram_user_id(message.get("from") or {})
    date_iso = datetime.fromtimestamp(message["date"], tz=timezone.utc).astimezone(VL).isoformat()

    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, entry_id, user_id, date_iso, parsed)
        conn.commit()
    _sync_to_sheet(entry_id, user_id, date_iso, parsed)

    send_message(message["chat"]["id"], _confirmation_text(parsed, entry_id), reply_markup=_confirmation_buttons(entry_id))


def _handle_edit_reply(message: dict) -> None:
    reply_text = (message.get("reply_to_message") or {}).get("text") or ""
    _prep_entry_id, old_description = fd.extract_edit_context(reply_text)
    correction = message.get("text") or ""
    prompt = fd.build_edit_prompt(old_description, correction)
    raw = call_text_llm(prompt)
    if not raw:
        send_message(message["chat"]["id"], "⚠️ Не смог пересчитать правку — модель не ответила. Попробуй ещё раз.")
        return
    parsed = fd.parse_json_from_ai(raw)
    parsed.update(fd.normalize_food_group_tags(parsed))

    # Порт "Только Обновление"/PG: финальный Entry_ID — из ТОГО ЖЕ текста, но
    # нестрогим регэкспом (не обязательно квадратные скобки), независимо от
    # prep-извлечения выше (тот же дубль-расчёт, что в оригинале).
    entry_id = fd.extract_loose_entry_id(reply_text)
    if not entry_id:
        send_message(message["chat"]["id"], "⚠️ Не нашёл номер записи в сообщении, на которое ты ответил — правка не сохранена.")
        return
    user_id = fd.telegram_user_id(message.get("from") or {})

    with get_conn() as conn, conn.cursor() as cur:
        fd.update_meal(cur, entry_id, user_id, parsed)
        conn.commit()
    _sync_to_sheet(entry_id, user_id, None, parsed)

    send_message(message["chat"]["id"], _confirmation_text(parsed, entry_id), reply_markup=_confirmation_buttons(entry_id))


def handle_message(message: dict) -> None:
    msg_type = fd.classify_message(message)
    if msg_type == "command":
        _handle_stats(message)
    elif msg_type == "reply":
        _handle_edit_reply(message)
    elif msg_type in ("text", "photo", "text_and_photo"):
        _handle_new_entry(message, msg_type)
    else:
        logger.info("food_diary_bot: сообщение не распознано ни в один тип, пропускаю")


def _chat_id_of(update: dict) -> str:
    if fd.is_callback(update):
        return str((((update.get("callback_query") or {}).get("message") or {}).get("chat") or {}).get("id", ""))
    return str(((update.get("message") or {}).get("chat") or {}).get("id", ""))


def handle_update(update: dict) -> None:
    # 2026-09-22 (внешний аудит, K5 — КРИТИЧНО): ничего здесь не проверяло
    # отправителя — любой, кто нашёл бота "vlad_health", писал бы себе в
    # health.meals как в дневник питания Влада. Отсекаем чужой chat_id
    # до классификации/записи.
    chat_id = _chat_id_of(update)
    if chat_id and chat_id != OWNER_CHAT_ID:
        logger.warning("food_diary_bot: апдейт %s от чужого chat_id=%s — игнорирую",
                        update.get("update_id"), chat_id)
        return
    if fd.is_callback(update):
        handle_callback(update["callback_query"])
    elif update.get("message"):
        handle_message(update["message"])


# =====================================================================
# Цикл опроса
# =====================================================================

def _safe_process(update: dict) -> None:
    try:
        handle_update(update)
    except Exception:
        logger.exception("food_diary_bot: необработанная ошибка на апдейте %s — апдейт потерян, приём продолжается",
                          update.get("update_id"))


def run_polling_loop() -> None:
    delete_webhook()
    with get_conn() as conn, conn.cursor() as cur:
        offset = _get_last_offset(cur)
    logger.info("food_diary_bot polling loop starting from offset %s", offset)

    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:
            logger.exception("food_diary_bot: getUpdates упал, повтор через 5с")
            alert_on_failure("food_diary_bot_poller", e)
            time.sleep(5)
            continue

        run_log.mark_run("food_diary_bot_poller", min_interval_seconds=300)
        for update in updates:
            _safe_process(update)
            offset = update["update_id"] + 1
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    _save_offset(cur, offset)
                    conn.commit()
            except Exception:
                logger.exception("food_diary_bot: не удалось сохранить offset %s", offset)
