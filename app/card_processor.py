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
просто выбирает необработанные id и не трогает статус сам.

F7 (внешний аудит логики, 2026-09-22): раньше упавшее сообщение навсегда
оставалось status='received' и переразбиралось КАЖДЫЕ 5 МИНУТ — бессрочно,
причём каждая попытка это LLM-вызов извлечения (деньги) и вечная ошибка в
логах. Теперь попытки считаются (`process_attempts` в card.source_message),
после MAX_ATTEMPTS сообщение уходит в dead-letter (status='failed', выпадает
из очереди) и владельцу уходит ОДИН алерт с id и последней ошибкой — сырьё цело
в карте, переиграть можно руками (write_path.process), ничего не потеряно.
"""
import logging
import time

from app import notify, run_log
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 5 * 60
BATCH_LIMIT = 20
MAX_ATTEMPTS = 3  # F7: после стольких неудач — dead-letter, не вечный ретрай


def get_pending_ids(cur) -> list[str]:
    cur.execute(
        f"SELECT id FROM {schema()}.source_message WHERE status = 'received' "
        "ORDER BY ts_received LIMIT %s",
        (BATCH_LIMIT,),
    )
    return [r[0] for r in cur.fetchall()]


def _mark_failure(source_id: str, exc: BaseException) -> int:
    """Посчитать неудачную попытку; на MAX_ATTEMPTS — dead-letter. Возвращает
    число попыток (0, если строка уже исчезла)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {schema()}.source_message "
            "SET process_attempts = process_attempts + 1, process_error = %s "
            "WHERE id = %s RETURNING process_attempts",
            (str(exc)[:500], source_id),
        )
        row = cur.fetchone()
        attempts = int(row[0]) if row else 0
        if attempts >= MAX_ATTEMPTS:
            cur.execute(
                f"UPDATE {schema()}.source_message SET status = 'failed' WHERE id = %s",
                (source_id,),
            )
        conn.commit()
    return attempts


def _alert_dead_letter(source_id: str, attempts: int, exc: BaseException) -> None:
    """Один алерт владельцу при уходе сообщения в dead-letter (fail-safe)."""
    try:
        notify.notify(
            "card_processor_dead_letter", "critical",
            f"💀 Сообщение {source_id} не разобралось за {attempts} попытки — убрано из очереди "
            f"(status=failed), вечного ретрая больше нет.\n"
            f"Последняя ошибка: {str(exc)[:300]}\n"
            "Текст цел в card.source_message; переиграть можно так: "
            "docker exec card-service python -c \"from app.write_path import process; print(process('<id>'))\"",
        )
    except Exception:
        logger.exception("card_processor: не удалось отправить алерт о dead-letter %s", source_id)


def run_once() -> None:
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
        except Exception as e:
            logger.exception("card_processor: process(%s) упал — попытка посчитана, повтор на следующем тике",
                             source_id)
            try:
                attempts = _mark_failure(source_id, e)
                if attempts >= MAX_ATTEMPTS:
                    _alert_dead_letter(source_id, attempts, e)
            except Exception:
                logger.exception("card_processor: не удалось отметить неудачную попытку для %s", source_id)

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
