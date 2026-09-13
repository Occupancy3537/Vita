from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def test_nutrition_facts_written_confirmed():
    r = client.post("/facts/nutrition", json={"facts": [
        {"metric_key": "nutrient:Клетчатка", "value_num": 28.5, "ts_event": "2026-09-13T00:00:00Z"},
    ]})
    assert r.status_code == 200
    assert r.json() == {"written": 1, "skipped_duplicate": 0}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT value_num, verification, provenance->>'origin' FROM {schema()}.fact WHERE metric_key = 'nutrient:Клетчатка'")
        row = cur.fetchone()
        assert float(row[0]) == 28.5 and row[1] == "confirmed" and row[2] == "nutrition"


def test_nutrition_facts_idempotent_on_rerun():
    payload = {"facts": [{"metric_key": "nutrient:Calories", "value_num": 2100, "ts_event": "2026-09-13T00:00:00Z"}]}
    r1 = client.post("/facts/nutrition", json=payload)
    r2 = client.post("/facts/nutrition", json=payload)
    assert r1.json() == {"written": 1, "skipped_duplicate": 0}
    assert r2.json() == {"written": 0, "skipped_duplicate": 1}


def test_nutrition_and_device_dedup_are_independent():
    """У nutrition и device — раздельные частичные индексы: один и тот же
    (metric_key, ts_event) может законно существовать в обоих origin одновременно
    (это разные факты, просто с совпадающим ключом/днём)."""
    same_payload_shape = {"metric_key": "shared_key_test", "value_num": 1, "ts_event": "2026-09-13T00:00:00Z"}
    r1 = client.post("/facts/device", json={"facts": [same_payload_shape]})
    r2 = client.post("/facts/nutrition", json={"facts": [same_payload_shape]})
    assert r1.json()["written"] == 1
    assert r2.json()["written"] == 1
