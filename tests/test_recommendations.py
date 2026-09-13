"""End-to-end: sync -> facts accumulate -> evaluate -> loops. Проверяет весь
Phase 3 конвейер через HTTP, не только verdict_engine изолированно (уже покрыт
test_verdict_engine.py)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)

STARTED = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _seed_facts(metric_key, day_values, base=STARTED):
    with get_conn() as conn, conn.cursor() as cur:
        for d, v in day_values:
            cur.execute(
                f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
                f"VALUES (%s, %s, %s, 'confirmed', %s, %s)",
                (f"f_seed_{d}_{metric_key}_{v}", base + timedelta(days=d), '{"origin":"device"}', metric_key, v),
            )
        conn.commit()


def test_sync_creates_recommendation_and_expectation_when_measurable():
    r = client.post("/recommendations/sync", json={
        "title": "Стабилизировать шаги", "rationale": "снизить осевую нагрузку",
        "source_ref": "rec_test_1", "started_ts": STARTED.isoformat(),
        "metric_key": "test_steps", "metric_label": "Шаги", "direction": "up", "magnitude": 500,
        "window_days": 7, "lag_days": 1, "baseline_days": 7,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True and body["measurable"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.expectation WHERE rec_id = %s", (body["id"],))
        assert cur.fetchone()[0] == 1


def test_sync_without_metric_is_not_measurable_no_expectation():
    r = client.post("/recommendations/sync", json={
        "title": "Держать текущий режим", "source_ref": "rec_test_2", "started_ts": STARTED.isoformat(),
    })
    assert r.json()["measurable"] is False
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.expectation WHERE rec_id = %s", (r.json()["id"],))
        assert cur.fetchone()[0] == 0


def test_sync_idempotent_on_reupload():
    payload = {"title": "X", "source_ref": "rec_test_3", "started_ts": STARTED.isoformat()}
    r1 = client.post("/recommendations/sync", json=payload)
    r2 = client.post("/recommendations/sync", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["created"] is False


def test_full_pipeline_sync_then_evaluate_then_loops():
    _seed_facts("test_hrv", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "Отбой в 23:00 семь дней", "rationale": "ожидаем рост ВСР",
        "source_ref": "rec_test_full", "started_ts": STARTED.isoformat(),
        "metric_key": "test_hrv", "metric_label": "ВСР ночью", "unit": "мс",
        "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0, "baseline_days": 7,
    })
    rec_id = sync_r.json()["id"]

    eval_r = client.post(f"/recommendations/{rec_id}/evaluate")
    assert eval_r.json()["evaluated"] is True
    assert eval_r.json()["verdict"] == "effective"

    loops_r = client.get("/recommendations/loops")
    assert loops_r.status_code == 200
    loops = loops_r.json()
    assert len(loops) == 1
    assert loops[0]["metric"] == "test_hrv"
    assert loops[0]["judgment"] == "good"
    assert loops[0]["status"] == "сработало"
    assert loops[0]["before"] == 40.0 and loops[0]["after"] == 46.0


def test_evaluate_unmeasurable_recommendation_returns_honest_reason():
    sync_r = client.post("/recommendations/sync", json={"title": "Y", "source_ref": "rec_test_unmeas", "started_ts": STARTED.isoformat()})
    r = client.post(f"/recommendations/{sync_r.json()['id']}/evaluate")
    assert r.json()["evaluated"] is False
    assert "не измерима" in r.json()["reason"]


def test_loops_excludes_data_gap_verdicts():
    """data_gap не должен засорять дашборд как ложный вердикт."""
    sync_r = client.post("/recommendations/sync", json={
        "title": "Z", "source_ref": "rec_test_gap", "started_ts": STARTED.isoformat(),
        "metric_key": "test_gap_metric", "direction": "up", "magnitude": 5,
    })
    client.post(f"/recommendations/{sync_r.json()['id']}/evaluate")  # нет фактов -> data_gap
    loops = client.get("/recommendations/loops").json()
    assert not any(l["metric"] == "test_gap_metric" for l in loops)


def test_reevaluate_supersedes_previous_verdict_not_duplicates():
    _seed_facts("test_reev", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "R", "source_ref": "rec_test_reev", "started_ts": STARTED.isoformat(),
        "metric_key": "test_reev", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    client.post(f"/recommendations/{rec_id}/evaluate")
    client.post(f"/recommendations/{rec_id}/evaluate")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.recommendation_verdict WHERE rec_id = %s AND status = 'current'", (rec_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.recommendation_verdict WHERE rec_id = %s", (rec_id,))
        assert cur.fetchone()[0] == 2  # одна current + одна superseded
