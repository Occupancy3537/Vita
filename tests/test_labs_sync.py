from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def test_visit_sync_creates_new_then_idempotent():
    payload = {"source_ref": "V20260914", "title": "Инвитро", "ts_event": "2026-09-14T00:00:00Z"}
    r1 = client.post("/visits/sync", json=payload)
    r2 = client.post("/visits/sync", json=payload)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert r1.json()["id"] == r2.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260914'")
        assert cur.fetchone()[0] == 1


def test_labs_result_creates_visit_and_lab_result_and_fact():
    r = client.post("/labs/result", json={
        "visit_source_ref": "V20260914b",
        "visit_ts_event": "2026-09-14T00:00:00Z",
        "marker_key": "M041",
        "marker_label": "Гемоглобин",
        "value_num": 145,
        "unit": "г/л",
        "ref_min": 130,
        "ref_max": 160,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE id = %s", (body["visit_id"],))
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT marker_label, value_num FROM {schema()}.lab_result WHERE id = %s", (body["id"],))
        row = cur.fetchone()
        assert row[0] == "Гемоглобин" and float(row[1]) == 145
        cur.execute(f"SELECT value_num FROM {schema()}.fact WHERE metric_key = 'lab:M041'")
        assert float(cur.fetchone()[0]) == 145


def test_labs_result_idempotent_on_reupload_same_document():
    payload = {
        "visit_source_ref": "V20260914c", "visit_ts_event": "2026-09-14T00:00:00Z",
        "marker_key": "M003", "value_num": 5.2,
    }
    r1 = client.post("/labs/result", json=payload)
    r2 = client.post("/labs/result", json=payload)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert r1.json()["id"] == r2.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result WHERE marker_key = 'M003'")
        assert cur.fetchone()[0] == 1
        # ровно один визит на этот source_ref, не два
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260914c'")
        assert cur.fetchone()[0] == 1


def test_labs_result_two_markers_same_visit_share_one_visit_row():
    client.post("/labs/result", json={"visit_source_ref": "V_shared", "visit_ts_event": "2026-09-14T00:00:00Z", "marker_key": "M001", "value_num": 10})
    r2 = client.post("/labs/result", json={"visit_source_ref": "V_shared", "visit_ts_event": "2026-09-14T00:00:00Z", "marker_key": "M002", "value_num": 20})
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V_shared'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result WHERE visit_id = %s", (r2.json()["visit_id"],))
        assert cur.fetchone()[0] == 2
