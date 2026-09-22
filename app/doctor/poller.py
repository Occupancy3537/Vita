"""
Long polling Telegram (план §3.2 шаг 2, 2026-09-16): card-service забирает
приём апдейтов себе полностью — по решению Влада n8n больше не видит Telegram
первым ни разу. dispatch.route() решает "доктору или нет"; докторские апдейты
обрабатываются прямо здесь (intake.handle_update); "registrar"/"anamnesis" —
тоже в этом же процессе (registrar.handle_update/anamnesis.handle_reply).

"other" (2026-09-21, AGENT_SYNC #38/#43 — независимый аудит ZCode, реальная
находка «тихая потеря данных»): раньше пересылалось на внутренний вебхук
n8n-Capitan (forward_to_capitan, снесено этим коммитом вместе с
CAPITAN_RELAY_URL/_extract_file_id). С момента отключения n8n это ВСЕГДА
падало (404/connection refused) — Владу уходило «⚠️ Доставка отключена
(Capitan выключен)», а сообщение реально терялось: TEST-классифицированный
текст («запиши холестерин 5.5») просто исчезал, хотя рабочий путь для него
уже был построен и простаивал — тот же /ingest, что использует garminbot,
плюс card_processor, который разбирает очередь каждые 5 минут. Мост снесён,
не починен: ingest_test_message() зовёт ingest() в процессе напрямую (без
HTTP-круга на себя же, тот же принцип, что и остальные внутренние вызовы
этой сессии). Фото/документ сюда не попадают вообще — dispatch.route()
отправляет их в "registrar" раньше, чем доходит до LLM-классификатора.

ВАЖНО: пока polling запущен, у бота НЕ должно быть зарегистрированного
Telegram-webhook (иначе getUpdates отвечает 409) — disable_telegram_webhook()
вызывается один раз при старте цикла.

Волна 1 (A3, 2026-09-17): (1) guard _safe_process вокруг обработки одного
апдейта — одно непредвиденное исключение больше не убивает поток приёма до
рестарта контейнера; (2) при сбое сохранения (см. ingest_test_message выше)
Владу уходит видимое «НЕ сохранено» вместо тихой потери, не чаще 1 сообщения
в 6 ч (анти-спам).
"""
import logging
import time

import httpx

from app import registrar
from app.db import get_conn, schema
from app.doctor import anamnesis, dispatch, intake, telegram
from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT = 30

# A3: видимая потеря вместо тихой. Влад (тот же доктор-бот), анти-спам 6 ч.
OWNER_CHAT_ID = "8956401"
LOSS_NOTIFY_COOLDOWN = 6 * 3600
_last_loss_notify_ts = 0.0  # модульное состояние; рестарт контейнера сбрасывает — допустимо


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


def _loss_summary(update: dict) -> str:
    """Первые 60 символов текста (включая caption фото), либо тип вложения."""
    msg = update.get("message") or {}
    text = msg.get("text") or msg.get("caption")
    if text:
        return str(text)[:60]
    if msg.get("photo"):
        return "фото"
    if msg.get("document"):
        return "документ"
    return "(без текста)"


def _notify_owner_lost(update: dict) -> None:
    """Видимая потеря: короткое сообщение Владу, что апдейт НЕ сохранён.
    Анти-спам — не чаще 1 сообщения в 6 ч. Сама отправка обёрнута в try/except:
    никогда не должна ронять вызывающий цикл."""
    global _last_loss_notify_ts
    now = time.time()
    if now - _last_loss_notify_ts < LOSS_NOTIFY_COOLDOWN:
        return
    _last_loss_notify_ts = now  # фиксируем ДО отправки: даже упавшая попытка не должна спамить ретраями
    try:
        telegram.send_message(
            OWNER_CHAT_ID,
            f"⚠️ Не удалось сохранить сообщение — НЕ сохранено: {_loss_summary(update)}",
        )
        logger.error("owner notified about LOST update %s (relay unavailable)", update.get("update_id"))
    except Exception:
        logger.exception(
            "failed to notify owner about lost update %s (cooldown still consumed)", update.get("update_id"))


def ingest_test_message(update: dict) -> None:
    """"other" из dispatch.route() — сегодня это ВСЕГДА TEST-классифицированный
    текст (фото/документ уже отфильтрованы в "registrar" раньше, до LLM).
    Сохраняет тот же путь, что garminbot и любой другой источник: /ingest
    (дедуп по hash, идемпотентно) -> card.source_message -> card_processor
    разбирает очередь каждые 5 минут -> write_path.process(). Раньше это
    пересылалось в n8n-Capitan и терялось (см. докстринг модуля)."""
    msg = update.get("message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        logger.error("ingest_test_message: апдейт %s без текста — нечего сохранять", update.get("update_id"))
        _notify_owner_lost(update)
        return
    from app.main import IngestRequest, ingest  # ленивый импорт — app.main сама импортирует этот модуль
    try:
        result = ingest(IngestRequest(channel="telegram", raw_text=text, person_id="self"))
        logger.info("ingest_test_message: апдейт %s сохранён как %s (status=%s, dup=%s)",
                    update.get("update_id"), result.id, result.status, result.duplicate)
    except Exception:
        logger.exception("ingest_test_message: не удалось сохранить апдейт %s", update.get("update_id"))
        _notify_owner_lost(update)


def process_one(update: dict) -> None:
    # 2026-09-22 (внешний аудит, K5 — КРИТИЧНО): ничего в поллере/диспетчере/
    # интейке не проверяло, что сообщение реально от Влада — любой, кто нашёл
    # бота, обрабатывался как пациент, получал ответ доктора с полным досье
    # Влада в контексте (context.build_dossier — глобальный, person_id="self",
    # не привязан к конкретному chat_id отправителя). Первая же строка теперь
    # отсекает чужие chat_id ДО dispatch/intake — не только до записи в карту,
    # но и до траты денег на LLM-классификатор на чужое сообщение.
    chat_id = str(((update.get("message") or {}).get("chat") or {}).get("id", ""))
    if chat_id and chat_id != OWNER_CHAT_ID:
        logger.warning("process_one: апдейт %s от чужого chat_id=%s — игнорирую",
                        update.get("update_id"), chat_id)
        return
    try:
        destination = dispatch.route(update)
    except Exception:
        logger.exception("dispatch.route failed for update %s — falling back to ingest as TEST",
                          update.get("update_id"))
        destination = "other"
    if destination == "doctor":
        intake.handle_update(update)
    elif destination == "anamnesis":
        anamnesis.handle_reply(update)
    elif destination == "registrar":
        # Волна 3 (B2): фото/документ лабораторий — разбор в card-service,
        # раньше уходили в выключенный Capitan (тихая потеря).
        registrar.handle_update(update)
    else:
        ingest_test_message(update)


def _safe_process(update: dict) -> None:
    """Guard (A3): одно непредвиденное исключение в обработке апдейта не должно
    убивать поток приёма сообщений до рестарта контейнера. Логируем и идём дальше;
    offset ниже по циклу всё равно сдвинется — т.е. апдейт потерян ВИДИМО (в логах),
    но приём продолжается.

    F1 (внешний аудит логики, 2026-09-22): «видимо в логах» на практике означало
    «не видно никак» — логи проактивно никто не читает. Теперь потеря сообщения
    дополнительно уходит алертом владельцу — тем же каналом и с тем же дедупом
    (60 мин), что падения фоновых циклов: alert_on_failure."""
    try:
        process_one(update)
    except Exception as e:
        logger.exception("unhandled error while processing update %s — update lost, polling continues",
                          update.get("update_id"))
        alert_on_failure("doctor_update", e)


def run_polling_loop() -> None:
    disable_telegram_webhook()
    with get_conn() as conn, conn.cursor() as cur:
        offset = _get_last_offset(cur)
    logger.info("telegram polling loop starting from offset %s", offset)

    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:
            logger.exception("getUpdates failed, retrying in 5s")
            alert_on_failure("doctor_poller", e)
            time.sleep(5)
            continue

        run_log.mark_run("doctor_poller", min_interval_seconds=300)
        for update in updates:
            _safe_process(update)
            offset = update["update_id"] + 1
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    _save_offset(cur, offset)
                    conn.commit()
            except Exception:
                logger.exception("failed to persist offset %s — next restart may reprocess this update", offset)
