"""Gap 3 (CARD_ARCHITECTURE_PLAN_2026-09-13.md §5, П4) — слои памяти, retrieval,
get_context(). Честно: golden-корпус здесь — стартовый (заземлён на реальных
сущностях/словах из уже существующих тестов проекта), не 100+ пар из спеки —
как и с красными флагами, полный корпус строится вместе с Владом, не в одиночку."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import memory
from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def _insert_bracelet_fact(metric_key: str, text: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, salience, metric_key, value_text) "
            f"VALUES (%s, now(), '{{}}', 'confirmed', 'bracelet', %s, %s)",
            (f"f_bracelet_{metric_key}", metric_key, text),
        )
        conn.commit()


def test_render_bracelet_empty():
    with get_conn() as conn, conn.cursor() as cur:
        text = memory.render_bracelet(cur)
    assert "браслет пуст" in text
    assert text.startswith("[БРАСЛЕТ")


def test_render_bracelet_with_real_shaped_data():
    _insert_bracelet_fact("allergy:novocaine_anaphylaxis", "анафилаксия на новокаин")
    with get_conn() as conn, conn.cursor() as cur:
        text = memory.render_bracelet(cur)
    assert "novocaine" in text or "новокаин" in text
    assert "confirmed" in text


def test_render_bracelet_version_is_stable_hash_of_content():
    """C2: одинаковый вход -> побайтово одинаковый рендер (кэшируемо по хешу)."""
    with get_conn() as conn, conn.cursor() as cur:
        t1 = memory.render_bracelet(cur)
        t2 = memory.render_bracelet(cur)
    assert t1 == t2


def test_render_hot_includes_active_problem_and_recommendation():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.problem (id, ts_event, provenance, verification, title, status, opened_ts) "
            f"VALUES ('pb_hot1', now(), '{{}}', 'confirmed', 'Тестовая проблема', 'active', now())"
        )
        cur.execute(
            f"INSERT INTO {schema()}.recommendation (id, ts_event, provenance, verification, title, status, started_ts, cycle) "
            f"VALUES ('rc_hot1', now(), '{{}}', 'confirmed', 'Тестовая рекомендация', 'active', now(), 1)"
        )
        conn.commit()
        text, overflowed = memory.render_hot(cur)
    assert "Тестовая проблема" in text
    assert "Тестовая рекомендация" in text
    assert overflowed is False


def test_render_hot_excludes_old_closed_episode():
    """§1.2: закрытые >14 дней не в горячем (не §6.1 форма — прямо в самом запросе)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.episode (id, ts_event, provenance, verification, symptom_key, status, end_ts) "
            f"VALUES ('ep_old', now() - interval '40 days', '{{}}', 'confirmed', 'old_symptom', 'resolved', now() - interval '30 days')"
        )
        conn.commit()
        text, _ = memory.render_hot(cur)
    assert "old_symptom" not in text


def test_render_hot_degrades_episodes_to_count_over_budget():
    with get_conn() as conn, conn.cursor() as cur:
        for i in range(5):
            cur.execute(
                f"INSERT INTO {schema()}.episode (id, ts_event, provenance, verification, symptom_key, status) "
                f"VALUES (%s, now(), '{{}}', 'confirmed', %s, 'open')",
                (f"ep_budget_{i}", f"symptom_{i}"),
            )
        conn.commit()
        text, overflowed = memory.render_hot(cur, budget_tokens=5)  # заведомо тесный бюджет
    assert overflowed is True
    assert "эпизодов" in text  # схлопнуто в счётчик, не перечислены все 5


def test_entity_index_populated_on_symptom_episode_create():
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит спина"})
    from app.extraction import Draft, ExtractionResult
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="back_pain", onset_expr="сейчас")]
    )):
        resp = client.post(f"/process/{r1.json()['id']}")
    ep_id = resp.json()["written"][0]["episode_id"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT object_id FROM {schema()}.entity_index WHERE entity_type='symptom' AND entity_value='back_pain'"
        )
        assert cur.fetchone()[0] == ep_id


def test_entity_index_populated_on_intervention_and_lab():
    client.post("/interventions/sync", json={
        "name": "Магний", "source_ref": "mem-test-iv-1", "started_ts": "2026-09-01T00:00:00Z",
    })
    client.post("/labs/result", json={
        "visit_source_ref": "mem-test-visit-1", "visit_ts_event": "2026-09-01T00:00:00Z",
        "marker_key": "ferritin", "value_num": 50,
    })
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.entity_index WHERE entity_type='substance' AND entity_value='магний'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.entity_index WHERE entity_type='lab_marker' AND entity_value='ferritin'")
        assert cur.fetchone()[0] == 1


def test_get_context_always_has_bracelet_and_missing_shape():
    """M4 + C1: браслет всегда есть (даже пустой), missing[] всегда все 4 поля,
    даже когда поиск не выполнялся (текст без сущностей)."""
    with get_conn() as conn, conn.cursor() as cur:
        ctx = memory.get_context(cur, "question", {"text": "как дела в целом"})
    assert ctx["bracelet"].startswith("[БРАСЛЕТ")
    assert ctx["hot"].startswith("[ГОРЯЧЕЕ")
    assert set(ctx["missing"].keys()) == {"searched", "found", "not_found", "depth_not_loaded", "extraction_confidence"}
    assert ctx["missing"]["searched"] == []  # "поиск не выполнялся", не "искали и не нашли"
    assert ctx["missing"]["extraction_confidence"] is None
    assert ctx["meta"]["renderer_version"] == memory.RENDERER_VERSION


def test_get_context_l1_dictionary_retrieval_finds_indexed_episode():
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит голова"})
    from app.extraction import Draft, ExtractionResult
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="headache")]
    )):
        client.post(f"/process/{r1.json()['id']}")

    with get_conn() as conn, conn.cursor() as cur:
        ctx = memory.get_context(cur, "question", {"text": "а что там с головой в этом месяце?"})
    assert "headache" in ctx["missing"]["searched"]
    assert "headache" in ctx["missing"]["found"]
    assert any("headache" in item["rendered"] for item in ctx["cold"])
    assert ctx["missing"]["extraction_confidence"] == 1.0  # L1, не L2


def test_get_context_l2_llm_fallback_only_when_l1_empty():
    with patch("app.memory.resolve_entities_l2_llm", return_value=[("symptom", "headache")]) as mock_l2:
        with get_conn() as conn, conn.cursor() as cur:
            ctx = memory.get_context(cur, "question", {"text": "болит совершенно непонятная штука без словарных слов"})
    mock_l2.assert_called_once()
    assert ctx["meta"]["l2_used"] is True
    assert ctx["missing"]["extraction_confidence"] == 0.6


def test_get_context_not_found_when_entity_recognized_but_no_data():
    with get_conn() as conn, conn.cursor() as cur:
        ctx = memory.get_context(cur, "question", {"text": "как там локоть"})
    assert "elbow_pain" in ctx["missing"]["searched"]
    assert "elbow_pain" in ctx["missing"]["not_found"]
    assert ctx["cold"] == []


def test_rehydration_endpoint_returns_full_object():
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит спина сильно"})
    from app.extraction import Draft, ExtractionResult
    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="back_pain", intensity=7)]
    )):
        resp = client.post(f"/process/{r1.json()['id']}")
    ep_id = resp.json()["written"][0]["episode_id"]

    r = client.get(f"/objects/episode/{ep_id}")
    assert r.status_code == 200
    assert r.json()["symptom_key"] == "back_pain" and r.json()["intensity"] == 7


def test_rehydration_404_for_unknown_id():
    assert client.get("/objects/episode/ep_does_not_exist").status_code == 404


def test_access_metrics_increment_on_retrieval():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.memory_note (id, provenance, verification, type, title, content, subject) "
            f"VALUES ('mn_access1', '{{}}', 'auto', 'preference', 'Тест', '{{}}', "
            f"'[{{\"entity_type\":\"symptom\",\"entity_value\":\"headache\"}}]')"
        )
        memory.index_entity(cur, "symptom", "headache", "mn_access1", "memory_note")
        conn.commit()

        cur.execute(f"SELECT access_count FROM {schema()}.memory_note WHERE id='mn_access1'")
        assert cur.fetchone()[0] == 0

        memory.get_context(cur, "question", {"text": "что там с головой"})
        conn.commit()

        cur.execute(f"SELECT access_count, last_accessed FROM {schema()}.memory_note WHERE id='mn_access1'")
        count, last_accessed = cur.fetchone()
    assert count == 1 and last_accessed is not None


def test_clinical_note_auto_created_on_no_effect_verdict():
    r = client.post("/recommendations/propose", json={
        "title": "Проверка clinical-заметки", "source_ref": "mem-test-clinical",
        "started_ts": "2026-01-01T00:00:00Z", "metric_key": "test_metric_clinical",
        "direction": "up", "magnitude": 999.0, "window_days": 7, "baseline_days": 7,
    })
    # завышенный magnitude мог бы упасть на G1 — используем прямой sync, не propose,
    # чтобы изолированно проверить именно create_clinical_note, а не ворота.
    from app.recommendations import RecommendationSyncRequest, sync_recommendation, evaluate_recommendation
    sync_req = RecommendationSyncRequest(
        title="Проверка clinical-заметки", source_ref="mem-test-clinical-2",
        started_ts="2026-01-01T00:00:00Z", metric_key="test_metric_clinical",
        direction="up", magnitude=5.0, window_days=7, baseline_days=7,
    )
    rc = sync_recommendation(sync_req)

    with get_conn() as conn, conn.cursor() as cur:
        # НЕТ фактов вообще -> data_gap, не no_effect/adverse -> заметка НЕ создаётся
        pass
    ev = evaluate_recommendation(rc.id)
    assert ev.verdict == "data_gap"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.memory_note WHERE type='clinical' AND content->>'rec_id'=%s", (rc.id,))
        assert cur.fetchone()[0] == 0  # data_gap не порождает clinical-заметку

    # Теперь даём факты, стабильно совпадающие с baseline (no_effect: дельта ~0).
    # По дню на каждый из 7 дней baseline и eval — покрытие 100%, выше порога 70%
    # (verdict_engine.COVERAGE_MIN) — 3 точки через день (как было раньше) не
    # дотягивали до порога и честно давали data_gap, что тоже проверено выше.
    with get_conn() as conn, conn.cursor() as cur:
        for day, val in [(-7, 10.0), (-6, 10.0), (-5, 10.0), (-4, 10.0), (-3, 10.0), (-2, 10.0), (-1, 10.0),
                          (1, 10.0), (2, 10.0), (3, 10.0), (4, 10.0), (5, 10.0), (6, 10.0), (7, 10.0)]:
            cur.execute(
                f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
                f"VALUES (%s, '2026-01-01'::date + (%s || ' days')::interval, '{{}}', 'confirmed', %s, %s)",
                (f"f_clin_{day}", day, "test_metric_clinical", val),
            )
        conn.commit()
    ev2 = evaluate_recommendation(rc.id)
    assert ev2.verdict == "no_effect"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT title FROM {schema()}.memory_note WHERE type='clinical' AND content->>'rec_id'=%s", (rc.id,))
        row = cur.fetchone()
    assert row is not None and "не помогает" in row[0]


def test_pre_archive_check_flags_critical_and_archives_quiet():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.episode (id, ts_event, provenance, verification, symptom_key, status, end_ts, context) "
            f"VALUES ('ep_quiet', now() - interval '200 days', '{{}}', 'confirmed', 'изжога', 'resolved', now() - interval '150 days', 'переел')"
        )
        cur.execute(
            f"INSERT INTO {schema()}.episode (id, ts_event, provenance, verification, symptom_key, status, end_ts, context) "
            f"VALUES ('ep_critical', now() - interval '200 days', '{{}}', 'confirmed', 'сыпь', 'resolved', now() - interval '150 days', 'после амоксициллина, аллергическая реакция')"
        )
        conn.commit()
        results = {r["episode_id"]: r for r in memory.run_pre_archive_check(cur)}
    assert results["ep_quiet"]["action"] == "archived"
    assert results["ep_critical"]["action"] == "w3_question"


# --- стартовый golden-корпус retrieval (не 100+, честно см. модуль docstring) ---
GOLDEN_RETRIEVAL_CORPUS = [
    ("болит спина после тренировки", [("symptom", "back_pain")]),
    ("реакция на новокаин у стоматолога", [("substance", "novocaine")]),
    ("аллергия на пенициллин была в детстве", [("substance", "penicillin")]),
    ("про грыжу поясничного отдела", [("problem", "l5_s1_hernia")]),
    ("пью магний перед сном", [("substance", "magnesium")]),
    ("витамин д принимаю уже месяц", [("substance", "vitamin_d")]),
    ("мигрень мучает третий день", [("symptom", "headache")]),
    ("изжога после ужина", [("symptom", "heartburn")]),
]


def test_golden_retrieval_corpus_l1():
    for text, expected in GOLDEN_RETRIEVAL_CORPUS:
        got = memory.resolve_entities_l1(text)
        for exp in expected:
            assert exp in got, f"'{text}' должен находить {exp}, нашёл {got}"
