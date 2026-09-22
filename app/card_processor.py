"""Порт n8n `Card Processor` (2026-09-21) — НАХОДКА при разборе «можно ли
полностью убрать n8n»: очередь `card.source_message` (сырьё, принятое
`/ingest`, ждущее /process) до сих пор опрашивалась n8n-воркфлоу каждые
5 минут (`SELECT id FROM card.source_message WHERE status='received' ...`
-> `POST http://card-service:8080/process/{id}`) — то есть СОБСТВЕННЫЙ
конвейер обработки card-service тихо зависел от n8n. Если бы n8n когда-либо
остановился, врач продолжал бы принимать сообщения (`/ingest` работает сам
по себе), но они никогда не доходили бы до извлечения/записи — накапливались
бы в status='received' без единой видимой ошибки.

Порт — прямой вызов process_source() в процессе вместо HTTP-круга на себя
же, тот же принцип, что и во всех остальных портах этой сессии. process()
сам помечает status='processed' в конце (app/write_path.py) — цикл здесь
просто выбирает необработанные id и не трогает статус сам."""
import logging
import time

from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 5 * 60
BATCH_LIMIT = 20


def get_pending_ids(cur) -> list[str]:
    from app.db import schema
    cur.execute(f"SELECT id FROM {schema()}.source_message WHERE status = 'received' ORDER BY ts_received LIMIT %s", (BATCH_LIMIT,))
    return [r[0] for r in cur.fetchall()]


def run_once() -> None:
    from app.db import get_conn
    from app.write_path import process as process_source

    with get_conn() as conn, conn.cursor() as cur:
        pending = get_pending_ids(cur)
    if not pending:
        return

    ok = 0
    for source_id in pending:
        try:
            process_source(source_id)
            ok += 1
        except Exception:
            logger.exception("card_processor: process(%s) упал — статус не продвинулся, повтор на следующем тике", source_id)

    logger.info("card_processor: обработано %d/%d сообщений", ok, len(pending))


def run_scheduler() -> None:
    logger.info("card_processor scheduler: старт (каждые %d мин)", INTERVAL_SECONDS // 60)
    while True:
        try:
            run_once()
            run_log.mark_run("card_processor")
        except Exception as e:
            logger.exception("card_processor: run_once упал целиком — повтор через обычный интервал")
            alert_on_failure("card_processor", e)
        time.sleep(INTERVAL_SECONDS)
