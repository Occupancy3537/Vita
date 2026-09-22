from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def test_ingest_writes_source_message():
    r = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит голова с утра"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("src_")
    assert body["status"] == "received"
    assert body["duplicate"] is False

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT raw_text, channel, status FROM {schema()}.source_message WHERE id = %s", (body["id"],))
        row = cur.fetchone()
    assert row == ("болит голова с утра", "telegram", "received")


def test_duplicate_raw_text_is_idempotent_not_a_new_row():
    """П1 edge-кейс: повторный ingest того же сырья — не ошибка и не вторая строка."""
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "то же самое сообщение"})
    r2 = client.post("/ingest", json={"channel": "telegram", "raw_text": "то же самое сообщение"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.source_message")
        (count,) = cur.fetchone()
    assert count == 1


def test_same_text_other_day_is_new_event(monkeypatch):
    """F10 (внешний аудит логики, 2026-09-22): дедуп-ключ = канал + день (в зоне
    человека) + текст — идентичный текст в ДРУГОЙ день это новое событие, а не
    «дубликат» давнего (раньше повторное «запиши вес 82» назавтра молча
    возвращало старую запись и ничего не сохраняло)."""
    from datetime import date
    import app.main as main_mod

    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "запиши вес 82"})
    assert r1.json()["duplicate"] is False

    monkeypatch.setattr(main_mod.timeutil, "today", lambda: date(1999, 1, 1))  # «другой день»
    r2 = client.post("/ingest", json={"channel": "telegram", "raw_text": "запиши вес 82"})
    assert r2.json()["duplicate"] is False
    assert r2.json()["id"] != r1.json()["id"]

    monkeypatch.undo()  # снова реальный день — тот же текст и день дедупятся
    r3 = client.post("/ingest", json={"channel": "telegram", "raw_text": "запиши вес 82"})
    assert r3.json()["duplicate"] is True
    assert r3.json()["id"] == r1.json()["id"]


def test_empty_raw_text_rejected():
    r = client.post("/ingest", json={"channel": "manual", "raw_text": "   "})
    assert r.status_code == 422


def test_unknown_channel_rejected():
    r = client.post("/ingest", json={"channel": "carrier_pigeon", "raw_text": "x"})
    assert r.status_code == 422
