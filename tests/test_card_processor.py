"""app/card_processor.py — порт n8n Card Processor (2026-09-21, находка при
проверке «можно ли убрать n8n»: card-service's own write-path queue was
being driven by n8n, not itself). Реальная схема card.source_message,
тестовые id, cleanup."""
import pytest

from app import card_processor as cp
from app.db import get_conn, schema


TEST_ID = "sm_test_cardprocessor_1"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema()}.source_message WHERE id = %s", (TEST_ID,))
        conn.commit()


def _insert_received(cur, raw_text="Тестовое сообщение для card_processor", entry_id=TEST_ID):
    cur.execute(
        f"INSERT INTO {schema()}.source_message (id, channel, raw_text, hash, status, ts_received) "
        "VALUES (%s, 'manual', %s, %s, 'received', now())",
        (entry_id, raw_text, f"hash-{entry_id}"),
    )


def test_get_pending_ids_finds_received_rows():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_received(cur)
        conn.commit()
        pending = cp.get_pending_ids(cur)
    assert TEST_ID in pending


def test_get_pending_ids_excludes_processed_rows():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_received(cur)
        cur.execute(f"UPDATE {schema()}.source_message SET status = 'processed' WHERE id = %s", (TEST_ID,))
        conn.commit()
        pending = cp.get_pending_ids(cur)
    assert TEST_ID not in pending


def test_run_once_calls_process_source_for_each_pending(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        _insert_received(cur)
        conn.commit()

    called = []
    monkeypatch.setattr("app.write_path.process", lambda source_id: called.append(source_id))
    cp.run_once()
    assert TEST_ID in called


def test_run_once_one_failure_does_not_block_others(monkeypatch):
    other_id = "sm_test_cardprocessor_2"
    with get_conn() as conn, conn.cursor() as cur:
        _insert_received(cur)
        _insert_received(cur, raw_text="ещё одно", entry_id=other_id)
        conn.commit()

    called = []

    def fake_process(source_id):
        if source_id == TEST_ID:
            raise RuntimeError("сбой обработки")
        called.append(source_id)

    monkeypatch.setattr("app.write_path.process", fake_process)
    try:
        cp.run_once()  # не должно бросить исключение наружу
        assert other_id in called
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {schema()}.source_message WHERE id = %s", (other_id,))
            conn.commit()


def test_run_once_empty_queue_no_crash():
    cp.run_once()


def test_failed_processing_counts_attempts_then_dead_letter(monkeypatch):
    """F7: неудачи считаются; после MAX_ATTEMPTS — dead-letter (status='failed',
    выпадает из очереди) и один алерт владельцу; до лимита — без алерта."""
    alerts = []
    monkeypatch.setattr(cp.notify, "notify",
                        lambda source, priority, text: alerts.append(text))
    monkeypatch.setattr("app.write_path.process",
                        lambda source_id: (_ for _ in ()).throw(RuntimeError("boom")))

    with get_conn() as conn, conn.cursor() as cur:
        _insert_received(cur)
        conn.commit()

    for expected in (1, 2):
        cp.run_once()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT status, process_attempts FROM {schema()}.source_message WHERE id = %s",
                        (TEST_ID,))
            status, attempts = cur.fetchone()
        assert status == "received" and attempts == expected
        assert alerts == []  # до лимита алертов нет

    cp.run_once()  # третья попытка — dead-letter
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, process_attempts, process_error FROM {schema()}.source_message WHERE id = %s",
                    (TEST_ID,))
        status, attempts, error = cur.fetchone()
    assert status == "failed" and attempts == cp.MAX_ATTEMPTS and "boom" in error
    assert len(alerts) == 1 and TEST_ID in alerts[0]

    with get_conn() as conn, conn.cursor() as cur:
        assert TEST_ID not in cp.get_pending_ids(cur)  # выпало из очереди, ретраев больше нет
