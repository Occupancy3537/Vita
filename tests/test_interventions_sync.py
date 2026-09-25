from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def test_intervention_sync_creates_new():
    r = client.post("/interventions/sync", json={
        "name": "Vitamin D3 5000 IU + K2 50 mcg (1 таблетка)",
        "source_ref": "calendar_recurring_abc123",
        "kind": "supplement",
        "started_ts": "2026-09-14T00:00:00Z",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT name, status, verification FROM {schema()}.intervention WHERE id = %s", (body["id"],))
        row = cur.fetchone()
        assert row == ("Vitamin D3 5000 IU + K2 50 mcg (1 таблетка)", "active", "confirmed")


def test_intervention_sync_stores_publication_id():
    """«Научный контур» (2026-09-25, Часть 5.3) — трассировка "откуда идея"."""
    r = client.post("/interventions/sync", json={
        "name": "Тест: интервенция из публикации",
        "source_ref": "test_pub_linkage_intervention",
        "kind": "supplement",
        "publication_id": "pub_test_linkage_001",
    })
    body = r.json()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT publication_id FROM {schema()}.intervention WHERE id = %s", (body["id"],))
        assert cur.fetchone()[0] == "pub_test_linkage_001"


def test_intervention_sync_idempotent_on_same_source_ref():
    """Повторный ежедневный прогон воркфлоу-синхронизатора не плодит вторую запись
    для того же календарного события."""
    payload = {"name": "Vitamin D3", "source_ref": "calendar_recurring_xyz", "kind": "supplement"}
    r1 = client.post("/interventions/sync", json=payload)
    r2 = client.post("/interventions/sync", json=payload)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert r1.json()["id"] == r2.json()["id"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.intervention WHERE name = 'Vitamin D3'")
        assert cur.fetchone()[0] == 1


def test_intervention_sync_different_source_refs_create_separate_rows():
    client.post("/interventions/sync", json={"name": "A", "source_ref": "ref_a"})
    client.post("/interventions/sync", json={"name": "B", "source_ref": "ref_b"})
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.intervention WHERE name IN ('A', 'B')")
        assert cur.fetchone()[0] == 2
