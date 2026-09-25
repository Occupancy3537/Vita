"""End-to-end: sync -> facts accumulate -> evaluate -> loops. Проверяет весь
Phase 3 конвейер через HTTP, не только verdict_engine изолированно (уже покрыт
test_verdict_engine.py)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import recommendations as rec
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


def test_sync_without_metric_is_not_measurable_but_gets_unmeasurable_expectation():
    """«Петля исходов» (2026-09-24): раньше "неизмеримо" значило НОЛЬ строк
    expectation — неотличимо от "забыли". Теперь всегда ровно одна primary
    ex_-строка, здесь type='unmeasurable' — виден на витрине, не тишина."""
    r = client.post("/recommendations/sync", json={
        "title": "Держать текущий режим", "source_ref": "rec_test_2", "started_ts": STARTED.isoformat(),
    })
    assert r.json()["measurable"] is False
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT type, metric_key, reason FROM {schema()}.expectation WHERE rec_id = %s", (r.json()["id"],))
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "unmeasurable" and rows[0][1] is None and rows[0][2]


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


# ─────── Автоматическая оценка (петля исходов, аудит логики 2026-09-23) ───────
# НАХОДКА: движок был построен целиком, но ничто его не вызывало — только
# ручной POST /recommendations/{id}/evaluate. find_due_recommendations()/
# run_once() — то, что теперь дёргает его само.

def test_find_due_recommendations_includes_closed_window_no_verdict():
    sync_r = client.post("/recommendations/sync", json={
        "title": "Due1", "source_ref": "rec_test_due1", "started_ts": STARTED.isoformat(),
        "metric_key": "test_due1", "direction": "up", "magnitude": 5, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        due = rec.find_due_recommendations(cur)
    assert rec_id in due


def test_find_due_recommendations_excludes_open_window():
    sync_r = client.post("/recommendations/sync", json={
        "title": "NotDue", "source_ref": "rec_test_notdue", "started_ts": datetime.now(timezone.utc).isoformat(),
        "metric_key": "test_notdue", "direction": "up", "magnitude": 5, "window_days": 7, "lag_days": 1,
    })
    rec_id = sync_r.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        due = rec.find_due_recommendations(cur)
    assert rec_id not in due


def test_find_due_recommendations_excludes_already_evaluated():
    _seed_facts("test_due_eval", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "Evaluated", "source_ref": "rec_test_due_eval", "started_ts": STARTED.isoformat(),
        "metric_key": "test_due_eval", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    client.post(f"/recommendations/{rec_id}/evaluate")
    with get_conn() as conn, conn.cursor() as cur:
        due = rec.find_due_recommendations(cur)
    assert rec_id not in due


def test_find_due_recommendations_retries_data_gap():
    """data_gap — не хватило данных на момент прошлой попытки, а не "готово
    навсегда" — должна остаться в очереди на повтор."""
    sync_r = client.post("/recommendations/sync", json={
        "title": "GapRetry", "source_ref": "rec_test_gap_retry", "started_ts": STARTED.isoformat(),
        "metric_key": "test_gap_retry_metric", "direction": "up", "magnitude": 5,
    })
    rec_id = sync_r.json()["id"]
    client.post(f"/recommendations/{rec_id}/evaluate")  # нет фактов -> data_gap
    with get_conn() as conn, conn.cursor() as cur:
        due = rec.find_due_recommendations(cur)
    assert rec_id in due


def test_run_once_evaluates_due_recommendations():
    _seed_facts("test_run_once", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "RunOnce", "source_ref": "rec_test_run_once", "started_ts": STARTED.isoformat(),
        "metric_key": "test_run_once", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]

    rec.run_once()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT verdict FROM {schema()}.recommendation_verdict WHERE rec_id = %s AND status = 'current'", (rec_id,))
        row = cur.fetchone()
    assert row is not None and row[0] == "effective"


def test_run_once_one_failure_does_not_block_others(monkeypatch):
    """Сбой оценки одной рекомендации не должен ронять весь тик — остальные
    due-рекомендации всё равно считаются."""
    _seed_facts("test_run_once_ok", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    ok_r = client.post("/recommendations/sync", json={
        "title": "RunOnceOK", "source_ref": "rec_test_run_once_ok", "started_ts": STARTED.isoformat(),
        "metric_key": "test_run_once_ok", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    ok_id = ok_r.json()["id"]

    real_evaluate = rec.evaluate_recommendation

    def flaky(rec_id):
        if rec_id != ok_id:
            raise RuntimeError("бум")
        return real_evaluate(rec_id)

    monkeypatch.setattr(rec, "evaluate_recommendation", flaky)
    bad_r = client.post("/recommendations/sync", json={
        "title": "RunOnceBad", "source_ref": "rec_test_run_once_bad", "started_ts": STARTED.isoformat(),
        "metric_key": "test_run_once_bad", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })

    rec.run_once()  # не бросает, несмотря на flaky() для bad_r

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.recommendation_verdict WHERE rec_id = %s", (ok_id,))
        assert cur.fetchone()[0] == 1


# ─────── topic_key / supersede / close (петля исходов, часть 4) ───────

def test_topic_key_supersedes_previous_active_recommendation_same_topic():
    r1 = client.post("/recommendations/sync", json={
        "title": "Снизить суточное потребление насыщенных жиров до нормы",
        "source_ref": "rec_topic_1", "started_ts": STARTED.isoformat(),
    })
    id1 = r1.json()["id"]
    r2 = client.post("/recommendations/sync", json={
        "title": "Снизить насыщенные жиры до ~28 г/день",
        "source_ref": "rec_topic_2", "started_ts": (STARTED + timedelta(days=14)).isoformat(),
    })
    id2 = r2.json()["id"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, superseded_by, stop_reason FROM {schema()}.recommendation WHERE id = %s", (id1,))
        status1, superseded_by1, stop_reason1 = cur.fetchone()
        assert status1 == "superseded" and superseded_by1 == id2 and stop_reason1 == "superseded_by_topic"
        cur.execute(f"SELECT status FROM {schema()}.recommendation WHERE id = %s", (id2,))
        assert cur.fetchone()[0] == "active"


def test_topic_key_different_topics_do_not_supersede():
    r1 = client.post("/recommendations/sync", json={
        "title": "Стабилизировать суточный шаг до 12-13 тысяч",
        "source_ref": "rec_topic_steps", "started_ts": STARTED.isoformat(),
    })
    r2 = client.post("/recommendations/sync", json={
        "title": "Снизить насыщенные жиры до ~28 г/день",
        "source_ref": "rec_topic_fat", "started_ts": STARTED.isoformat(),
    })
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {schema()}.recommendation WHERE id = %s", (r1.json()["id"],))
        assert cur.fetchone()[0] == "active"


def test_close_recommendation_direct_function_marks_closed_with_reason():
    r = client.post("/recommendations/sync", json={
        "title": "Разовая", "source_ref": "rec_close_direct", "started_ts": STARTED.isoformat(),
    })
    rec_id = r.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        ok = rec.close_recommendation(cur, rec_id, "выполнено вручную")
        conn.commit()
        assert ok is True
        cur.execute(f"SELECT status, stop_reason FROM {schema()}.recommendation WHERE id = %s", (rec_id,))
        assert cur.fetchone() == ("closed", "выполнено вручную")

        # повторное закрытие уже закрытой — честный False, не второй journal-повтор
        assert rec.close_recommendation(cur, rec_id) is False
        conn.commit()


def test_evaluate_closes_recommendation_on_no_effect_verdict():
    _seed_facts("test_close_ne", [(-i, 38 + (i % 5)) for i in range(8, 91)])
    _seed_facts("test_close_ne", [(-i, 40) for i in range(1, 8)] + [(i, 39) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "ЗакрытьNoEffect", "source_ref": "rec_close_ne", "started_ts": STARTED.isoformat(),
        "metric_key": "test_close_ne", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    eval_r = client.post(f"/recommendations/{rec_id}/evaluate")
    assert eval_r.json()["verdict"] == "no_effect"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, stop_reason FROM {schema()}.recommendation WHERE id = %s", (rec_id,))
        assert cur.fetchone() == ("closed", "no_effect")


def test_evaluate_keeps_recommendation_active_on_effective_verdict():
    _seed_facts("test_stay_active", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    sync_r = client.post("/recommendations/sync", json={
        "title": "ОстатьсяАктивной", "source_ref": "rec_stay_active", "started_ts": STARTED.isoformat(),
        "metric_key": "test_stay_active", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    eval_r = client.post(f"/recommendations/{rec_id}/evaluate")
    assert eval_r.json()["verdict"] == "effective"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {schema()}.recommendation WHERE id = %s", (rec_id,))
        assert cur.fetchone()[0] == "active"


def test_evaluate_records_confounders_from_overlapping_active_intervention():
    _seed_facts("test_confound", [(-i, 40) for i in range(1, 8)] + [(i, 46) for i in range(0, 7)])
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.intervention (id, ts_event, provenance, verification, kind, name, status, started_ts) "
            f"VALUES ('iv_confound_test', now(), '{{}}', 'confirmed', 'supplement', 'Тестовая добавка', 'active', %s)",
            (STARTED - timedelta(days=3),),
        )
        conn.commit()
    sync_r = client.post("/recommendations/sync", json={
        "title": "СКонфаундером", "source_ref": "rec_confound", "started_ts": STARTED.isoformat(),
        "metric_key": "test_confound", "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    rec_id = sync_r.json()["id"]
    client.post(f"/recommendations/{rec_id}/evaluate")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT confounded, rule_trace FROM {schema()}.recommendation_verdict WHERE rec_id = %s", (rec_id,))
        confounded, rule_trace = cur.fetchone()
        assert "Тестовая добавка" in confounded
        assert "конфаундер" in rule_trace["confounder_note"]


# ─────── get_active_recommendations (петля исходов, часть 6) ───────

def test_active_recommendations_shows_expectation_or_unmeasurable_for_every_active_rec():
    r1 = client.post("/recommendations/sync", json={
        "title": "Измеримая", "source_ref": "rec_active_meas", "started_ts": STARTED.isoformat(),
        "metric_key": "test_active_metric", "metric_label": "Тестовая метрика", "unit": "ед",
        "direction": "up", "magnitude": 5, "window_days": 7,
    })
    r2 = client.post("/recommendations/sync", json={
        "title": "Неизмеримая", "source_ref": "rec_active_unmeas", "started_ts": STARTED.isoformat(),
        "unmeasurable_reason": "разовое действие",
    })
    active = client.get("/recommendations/active").json()
    by_id = {a["id"]: a for a in active}
    assert by_id[r1.json()["id"]]["is_unmeasurable"] is False
    assert "Тестовая метрика" in by_id[r1.json()["id"]]["summary"]
    assert by_id[r2.json()["id"]]["is_unmeasurable"] is True
    assert by_id[r2.json()["id"]]["summary"] == "неизмеримо: разовое действие"


def test_active_recommendations_excludes_closed():
    r = client.post("/recommendations/sync", json={
        "title": "БудетЗакрыта", "source_ref": "rec_active_closed", "started_ts": STARTED.isoformat(),
    })
    rec_id = r.json()["id"]
    with get_conn() as conn, conn.cursor() as cur:
        rec.close_recommendation(cur, rec_id, "тест")
        conn.commit()
    active = client.get("/recommendations/active").json()
    assert not any(a["id"] == rec_id for a in active)


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
