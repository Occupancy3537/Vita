"""
Long polling Telegram (план §3.2 шаг 2, 2026-09-16): card-service забирает
приём апдейтов себе полностью — по решению Влада n8n больше не видит Telegram
первым ни разу. dispatch.route() решает "доктору или нет"; докторские апдейты
обрабатываются прямо здесь (intake.handle_update, тот же код, что раньше
дёргал HTTP-хоп из Capitan — теперь вызывается напрямую, без сети); остальные
пересылаются на внутренний вебхук Capitan сырым Telegram-update — регистратор/
анамнез продолжают работать без единой строчки изменений в их собственной
логике.

Фото/документ пересылаются как multipart/form-data (поле "update" — JSON
апдейта строкой, поле "data" — скачанный файл), а не просто JSON: у старого
`Telegram Trigger` в Capitan было `additionalFields.download: true` —
автоскачивание файла в n8n-binary при живом вебхуке от Telegram. Раз Telegram
больше не стучится в n8n напрямую, этот шаг теперь должен сделать кто-то —
card-service (уже умеет, app.doctor.telegram.download_file — Phase 1) скачивает
сам и передаёт файл вместе с апдейтом; n8n Webhook-нода разбирает
multipart/form-data нативно (json.body.* — текстовые поля, binary[key] —
файлы), Capitan получает то же самое, что раньше давало автоскачивание.

ВАЖНО: пока polling запущен, у бота НЕ должно быть зарегистрированного
Telegram-webhook (иначе getUpdates отвечает 409) — disable_telegram_webhook()
вызывается один раз при старте цикла.
"""
import logging
import os
import time

import httpx

from app.db import get_conn, schema
from app.doctor import dispatch, intake, telegram

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
CAPITAN_RELAY_URL = os.environ.get("CAPITAN_RELAY_URL", "")
POLL_TIMEOUT = 30


def disable_telegram_webhook() -> None:
    try:
        resp = httpx.post(
            f"{TELEGRAM_API_BASE.format(token=telegram._token())}/deleteWebhook",
            json={"drop_pending_updates": False}, timeout=10,
        )
        resp.raise_for_status()
        logger.info("telegram webhook deleted: %s", resp.json())
    except Exception:
        logger.exception("failed to delete telegram webhook")


def _get_last_offset(cur) -> int:
    cur.execute(f"SELECT last_update_id FROM {schema()}.telegram_poll_state WHERE id = 'singleton'")
    row = cur.fetchone()
    return row[0] if row else 0


def _save_offset(cur, update_id: int) -> None:
    cur.execute(
        f"INSERT INTO {schema()}.telegram_poll_state (id, last_update_id, updated_at) "
        "VALUES ('singleton', %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET last_update_id = EXCLUDED.last_update_id, updated_at = now()",
        (update_id,),
    )


def get_updates(offset: int, timeout: float = POLL_TIMEOUT) -> list[dict]:
    resp = httpx.get(
        f"{TELEGRAM_API_BASE.format(token=telegram._token())}/getUpdates",
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data["result"]


def _extract_file_id(update: dict) -> tuple[str, str] | tuple[None, None]:
    """Возвращает (file_id, filename) для крупнейшей фото-версии или документа,
    или (None, None), если во апдейте нет вложения."""
    msg = update.get("message") or {}
    if msg.get("photo"):
        largest = msg["photo"][-1]  # Telegram отдаёт по возрастанию размера
        return largest["file_id"], f"{largest['file_id']}.jpg"
    if msg.get("document"):
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name") or doc["file_id"]
    return None, None


def forward_to_capitan(update: dict) -> None:
    if not CAPITAN_RELAY_URL:
        logger.error("CAPITAN_RELAY_URL не задан — апдейт %s потерян", update.get("update_id"))
        return
    import json
    file_id, filename = _extract_file_id(update)
    try:
        if file_id:
            content = telegram.download_file(file_id)
            files = {"data": (filename, content)}
            data = {"update": json.dumps(update, ensure_ascii=False)}
            resp = httpx.post(CAPITAN_RELAY_URL, data=data, files=files, timeout=20)
        else:
            resp = httpx.post(CAPITAN_RELAY_URL, data={"update": json.dumps(update, ensure_ascii=False)}, timeout=10)
        resp.raise_for_status()
    except Exception:
        logger.exception("failed to forward update %s to Capitan", update.get("update_id"))


def process_one(update: dict) -> None:
    try:
        destination = dispatch.route(update)
    except Exception:
        logger.exception("dispatch.route failed for update %s — forwarding to Capitan as fallback",
                          update.get("update_id"))
        destination = "other"
    if destination == "doctor":
        intake.handle_update(update)
    else:
        forward_to_capitan(update)


def run_polling_loop() -> None:
    disable_telegram_webhook()
    with get_conn() as conn, conn.cursor() as cur:
        offset = _get_last_offset(cur)
    logger.info("telegram polling loop starting from offset %s", offset)

    while True:
        try:
            updates = get_updates(offset)
        except Exception:
            logger.exception("getUpdates failed, retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            process_one(update)
            offset = update["update_id"] + 1
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    _save_offset(cur, offset)
                    conn.commit()
            except Exception:
                logger.exception("failed to persist offset %s — next restart may reprocess this update", offset)
