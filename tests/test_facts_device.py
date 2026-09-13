from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def test_device_facts_written_confirmed():
    r = client.post("/facts/device", json={"facts": [
        {"metric_key": "hrv", "value_num": 55.3, "ts_event": "2026-09-13T00:00:00Z"},
        {"metric_key": "rhr", "value_num": 48, "ts_event": "2026-09-13T00:00:00Z"},
    ]})
    assert r.status_code == 200
    assert r.json() == {"written": 2, "skipped_duplicate": 0}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT metric_key, value_num, verification FROM {schema()}.fact WHERE metric_key = 'hrv'")
        row = cur.fetchone()
        assert float(row[1]) == 55.3 and row[2] == "confirmed"


def test_device_facts_idempotent_on_rerun():
    """П2 §3.3 дедуп факты-устройства: (device, metric_key, ts_event) уникален —
    повторный прогон того же дня не плодит дубли."""
    payload = {"facts": [{"metric_key": "steps", "value_num": 12000, "ts_event": "2026-09-13T00:00:00Z"}]}
    r1 = client.post("/facts/device", json=payload)
    r2 = client.post("/facts/device", json=payload)
    assert r1.json() == {"written": 1, "skipped_duplicate": 0}
    assert r2.json() == {"written": 0, "skipped_duplicate": 1}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.fact WHERE metric_key = 'steps'")
        assert cur.fetchone()[0] == 1
