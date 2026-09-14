"""Gap 1 (CARD_ARCHITECTURE_PLAN_2026-09-13.md §5): card.journal/card.extraction
были пустыми таблицами несмотря на комментарии "-> journal" в коде. Эти тесты
проверяют, что запись теперь реально происходит — по каждой точке write-path,
не только там, где проще всего написать тест."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.extraction import Draft, ExtractionResult
from app.main import app

client = TestClient(app)


def _journal_rows(object_type: str, object_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT op, diff FROM {schema()}.journal WHERE object_type = %s AND object_id = %s ORDER BY ts",
            (object_type, object_id),
        )
        return cur.fetchall()


def test_device_fact_create_writes_journal_with_link_back():
    r = client.post("/facts/device", json={"facts": [
        {"metric_key": "hrv", "value_num": 55.3, "ts_event": "2026-09-13T00:00:00Z"},
    ]})
    assert r.status_code == 200

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id, journal_ref FROM {schema()}.fact WHERE metric_key = 'hrv'")
        fact_id, journal_ref = cur.fetchone()

    rows = _journal_rows("fact", fact_id)
    assert len(rows) == 1
    op, diff = rows[0]
    assert op == "create"
    assert diff["metric_key"] == "hrv" and diff["origin"] == "device"
    assert journal_ref is not None  # link_back проставил обратную ссылку


def test_device_fact_dedup_rerun_does_not_double_journal():
    payload = {"facts": [{"metric_key": "steps", "value_num": 12000, "ts_event": "2026-09-13T00:00:00Z"}]}
    client.post("/facts/device", json=payload)
    client.post("/facts/device", json=payload)  # повтор — дедуп, не должен добавить вторую строку журнала

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {schema()}.fact WHERE metric_key = 'steps'")
        fact_id = cur.fetchone()[0]
    assert len(_journal_rows("fact", fact_id)) == 1


def test_symptom_episode_create_then_close_writes_journal():
    """«болит -> прошло» — эпизод создаётся (journal op=create), закрывается
    (journal op=close) — та же последовательность, что тестирует dedup-логику
    write_path, но здесь смотрим именно на журнал."""
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит голова"})
    src1 = r1.json()["id"]
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="headache", onset_expr="сейчас")]
    )):
        resp1 = client.post(f"/process/{src1}")
    ep_id = resp1.json()["written"][0]["episode_id"]

    ep_rows = _journal_rows("episode", ep_id)
    assert len(ep_rows) == 1 and ep_rows[0][0] == "create"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT journal_ref FROM {schema()}.episode WHERE id = %s", (ep_id,))
        assert cur.fetchone()[0] is not None

    r2 = client.post("/ingest", json={"channel": "telegram", "raw_text": "голова прошла"})
    src2 = r2.json()["id"]
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="headache", negation=True)]
    )):
        resp2 = client.post(f"/process/{src2}")
    assert resp2.json()["written"][0]["action"] == "closed_episode"

    ep_rows = _journal_rows("episode", ep_id)
    assert [r[0] for r in ep_rows] == ["create", "close"]
    assert ep_rows[1][1]["status"] == "resolved"


def test_process_writes_extraction_row():
    r = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит спина"})
    src_id = r.json()["id"]
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="back_pain")], model="test-model"
    )):
        client.post(f"/process/{src_id}")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT source_id, model, status, drafts_json FROM {schema()}.extraction WHERE source_id = %s", (src_id,))
        row = cur.fetchone()
    assert row is not None, "process() должен писать card.extraction, а не только card.fact/episode"
    assert row[0] == src_id and row[1] == "test-model" and row[2] == "applied"
    assert row[3][0]["symptom_key"] == "back_pain"


def test_process_no_medical_content_extraction_status():
    r = client.post("/ingest", json={"channel": "telegram", "raw_text": "привет, как дела"})
    src_id = r.json()["id"]
    with patch("app.write_path.extract", return_value=ExtractionResult(no_medical_content=True, drafts=[])):
        client.post(f"/process/{src_id}")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {schema()}.extraction WHERE source_id = %s", (src_id,))
        assert cur.fetchone()[0] == "no_medical"


def test_recommendation_and_expectation_write_journal():
    r = client.post("/recommendations/sync", json={
        "title": "Магний глицинат", "source_ref": "test-rec-1", "started_ts": "2026-09-01T00:00:00Z",
        "metric_key": "sleep_min", "direction": "up", "magnitude": 20.0,
    })
    rc_id = r.json()["id"]
    assert r.json()["measurable"] is True

    rc_rows = _journal_rows("recommendation", rc_id)
    assert len(rc_rows) == 1 and rc_rows[0][0] == "create"
    assert rc_rows[0][1]["title"] == "Магний глицинат"

    with get_conn() as conn, conn.cursor() as cur:
        # expectation не имеет колонки journal_ref вообще (осознанно, см. journal.py
        # _HAS_JOURNAL_REF) — не запрашиваем её, только сам факт наличия записи.
        cur.execute(f"SELECT id FROM {schema()}.expectation WHERE rec_id = %s", (rc_id,))
        ex_id = cur.fetchone()[0]
        cur.execute(f"SELECT journal_ref FROM {schema()}.recommendation WHERE id = %s", (rc_id,))
        rc_journal_ref = cur.fetchone()[0]

    ex_rows = _journal_rows("expectation", ex_id)
    assert len(ex_rows) == 1 and ex_rows[0][0] == "create"
    assert rc_journal_ref is not None


def test_recommendation_sync_dedup_does_not_double_journal():
    payload = {"title": "X", "source_ref": "test-rec-dedup", "started_ts": "2026-09-01T00:00:00Z"}
    r1 = client.post("/recommendations/sync", json=payload)
    r2 = client.post("/recommendations/sync", json=payload)
    rc_id = r1.json()["id"]
    assert rc_id == r2.json()["id"]
    assert len(_journal_rows("recommendation", rc_id)) == 1


def test_evaluate_supersede_writes_update_then_create():
    r = client.post("/recommendations/sync", json={
        "title": "Тест-эффект", "source_ref": "test-rec-eval", "started_ts": "2026-09-01T00:00:00Z",
        "metric_key": "test_metric", "direction": "up", "magnitude": 5.0, "window_days": 7, "baseline_days": 7,
    })
    rc_id = r.json()["id"]

    with get_conn() as conn, conn.cursor() as cur:
        for day, val in [(-5, 10.0), (-3, 10.0), (-1, 10.0), (2, 20.0), (4, 20.0), (6, 20.0)]:
            cur.execute(
                f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
                f"VALUES (%s, '2026-09-01'::date + (%s || ' days')::interval, '{{}}', 'confirmed', %s, %s)",
                (f"f_evaltest_{day}", day, "test_metric", val),
            )
        conn.commit()

    ev1 = client.post(f"/recommendations/{rc_id}/evaluate")
    assert ev1.json()["evaluated"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {schema()}.recommendation_verdict WHERE rec_id = %s AND status = 'current'", (rc_id,))
        first_rv_id = cur.fetchone()[0]
    assert _journal_rows("recommendation_verdict", first_rv_id)[0][0] == "create"

    ev2 = client.post(f"/recommendations/{rc_id}/evaluate")
    assert ev2.json()["evaluated"] is True

    superseded_rows = _journal_rows("recommendation_verdict", first_rv_id)
    assert [r[0] for r in superseded_rows] == ["create", "update"]
    assert superseded_rows[1][1]["status"] == "superseded"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {schema()}.recommendation_verdict WHERE rec_id = %s AND status = 'current'", (rc_id,))
        second_rv_id = cur.fetchone()[0]
    assert second_rv_id != first_rv_id
    assert _journal_rows("recommendation_verdict", second_rv_id)[0][0] == "create"
