"""Gap 2 (CARD_ARCHITECTURE_PLAN_2026-09-13.md §5, П3 §2.2) — ворота G1-G6 на
рождении рекомендации. Канонические сценарии из самой спеки: G3 — "рекомендация
лидокаин-класса при браслетной анафилаксии не может быть записана вовсе"; G4 —
"изометрия при гипертоническом/ОДА-гейте блокирована". card_test пуст перед каждым
тестом (conftest.py) — браслет/gate заводим внутри теста, не полагаемся на прод-данные."""
from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def _seed_bracelet_fact(metric_key: str, text: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, salience, metric_key, value_text) "
            f"VALUES (%s, now(), '{{}}', 'confirmed', 'bracelet', %s, %s)",
            (f"f_bracelet_{metric_key}", metric_key, text),
        )
        conn.commit()


def _seed_metric_coverage(metric_key: str):
    """card_test начинается пустым каждый тест (в отличие от прод card, где Phase 1
    уже засеял hrv/rhr/sleep_min и т.п. наблюдателями) — тестам, которым нужен
    measurable=True, надо явно завести наблюдателя."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.metric_coverage (metric_key, observer, frequency) VALUES (%s, 'device', 'daily') "
            f"ON CONFLICT DO NOTHING",
            (metric_key,),
        )
        conn.commit()


def _seed_gate_problem(title: str, contra_load: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.problem (id, ts_event, provenance, verification, title, status, opened_ts, gate) "
            f"VALUES (%s, now(), '{{}}', 'confirmed', %s, 'active', now(), %s)",
            (f"pb_gatetest", title, __import__("json").dumps({"contra_load": contra_load})),
        )
        conn.commit()


BASE = {"title": "Т", "source_ref": "gate-test-1", "started_ts": "2026-09-01T00:00:00Z"}
# G7 (2026-09-24) требует ЛИБО ожидание, ЛИБО unmeasurable_reason — тесты G3-G6
# ниже проверяют ДРУГИЕ ворота, не G7, поэтому глушат его этим полем.
_UNMEASURABLE = {"unmeasurable_reason": "тест: G7 не в фокусе этого сценария"}


def test_g1_window_out_of_range_rejected():
    r = client.post("/recommendations/propose", json={**BASE, "window_days": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False and body["rejected_gate"] == "G1"


def test_g1_lag_exceeds_half_window_rejected():
    r = client.post("/recommendations/propose", json={**BASE, "window_days": 10, "lag_days": 6})
    assert r.json()["accepted"] is False and r.json()["rejected_gate"] == "G1"


def test_g1_magnitude_beyond_physiological_limit_rejected():
    r = client.post("/recommendations/propose", json={
        **BASE, "metric_key": "hrv", "direction": "up", "magnitude": 200.0,
    })
    assert r.json()["accepted"] is False and r.json()["rejected_gate"] == "G1"


def test_g1_valid_window_and_magnitude_passes():
    _seed_metric_coverage("hrv")
    r = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-g1-ok", "metric_key": "hrv", "direction": "up", "magnitude": 5.0,
    })
    assert r.json()["accepted"] is True and r.json()["measurable"] is True


def test_g2_unmeasurable_metric_degrades_not_blocks():
    """Метрика без наблюдателя в metric_coverage -> unmeasurable, НЕ reject (спека:
    G2 никогда не блокирует, только меняет режим)."""
    r = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-g2", "metric_key": "no_such_metric_anywhere",
        "direction": "up", "magnitude": 1.0,
    })
    body = r.json()
    assert body["accepted"] is True
    assert body["measurable"] is False  # ex_ не создан — unmeasurable


def test_g3_bracelet_intersection_blocks_recommendation():
    """Канонический сценарий спеки: рекомендация вещества из браслета анафилаксии не
    может быть записана вовсе, до всякой прозы."""
    _seed_bracelet_fact("allergy:novocaine_anaphylaxis", "анафилаксия на новокаин")
    r = client.post("/recommendations/propose", json={
        **BASE, **_UNMEASURABLE, "source_ref": "gate-test-g3", "title": "Обезболивание новокаином перед процедурой",
    })
    body = r.json()
    assert body["accepted"] is False and body["rejected_gate"] == "G3"
    assert "браслет" in body["rejected_reason"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.recommendation WHERE provenance->>'source_ref' = 'gate-test-g3'")
        assert cur.fetchone()[0] == 0, "заблокированная G3 рекомендация не должна создавать rc_"


def test_g4_gate_contradiction_blocks_recommendation():
    """Второй канонический сценарий: действие, противоречащее активному gate
    (ОДА-ограничение "статические удержания"), блокировано — та же семья, что
    "изометрия при гипертонии" из спеки, на реальных полях card.problem.gate."""
    _seed_gate_problem("Грыжа L5/S1", "статические удержания; осевая нагрузка")
    r = client.post("/recommendations/propose", json={
        **BASE, **_UNMEASURABLE, "source_ref": "gate-test-g4", "action": "Делать изометрические планки 3 раза в неделю",
    })
    body = r.json()
    assert body["accepted"] is False and body["rejected_gate"] == "G4"
    assert "L5/S1" in body["rejected_reason"]


def test_g4_unrelated_recommendation_not_blocked():
    _seed_gate_problem("Грыжа L5/S1", "статические удержания; осевая нагрузка")
    r = client.post("/recommendations/propose", json={
        **BASE, **_UNMEASURABLE, "source_ref": "gate-test-g4-ok", "action": "Пить больше воды утром",
    })
    assert r.json()["accepted"] is True


def test_g5_duplicate_kind_action_returns_reference_not_new_id():
    payload = {**BASE, **_UNMEASURABLE, "source_ref": "gate-test-g5-first", "kind": "behavior", "action": "Ходьба 12000 шагов"}
    r1 = client.post("/recommendations/propose", json=payload)
    assert r1.json()["accepted"] is True
    first_id = r1.json()["id"]

    r2 = client.post("/recommendations/propose", json={
        **payload, "source_ref": "gate-test-g5-second", "title": "Другая формулировка",
    })
    body2 = r2.json()
    assert body2["accepted"] is False and body2["rejected_gate"] == "G5"
    assert body2["duplicate_of"] == first_id


def test_g5_conflicting_direction_same_metric_rejected():
    _seed_metric_coverage("sleep_min")
    r1 = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-g5c-1", "metric_key": "sleep_min", "direction": "up", "magnitude": 20.0,
    })
    assert r1.json()["accepted"] is True

    r2 = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-g5c-2", "metric_key": "sleep_min", "direction": "down", "magnitude": 10.0,
    })
    body2 = r2.json()
    assert body2["accepted"] is False and body2["rejected_gate"] == "G5"
    assert body2["duplicate_of"] == r1.json()["id"]


def test_g6_priority_high_for_bioage_driver():
    r = client.post("/recommendations/propose", json={
        **BASE, **_UNMEASURABLE, "source_ref": "gate-test-g6-hi", "is_bioage_driver": True,
    })
    assert r.json()["accepted"] is True and r.json()["priority"] == "high"


def test_g6_priority_normal_by_default():
    r = client.post("/recommendations/propose", json={**BASE, **_UNMEASURABLE, "source_ref": "gate-test-g6-norm"})
    assert r.json()["accepted"] is True and r.json()["priority"] == "normal"


def test_g7_neither_expectation_nor_reason_rejected():
    """«Петля исходов» (2026-09-24): черновик без ожидания и без явной причины
    неизмеримости отклоняется — не создаёт rc_ вовсе (акс. критерий тикета)."""
    r = client.post("/recommendations/propose", json={**BASE, "source_ref": "gate-test-g7-reject"})
    body = r.json()
    assert body["accepted"] is False and body["rejected_gate"] == "G7"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.recommendation WHERE provenance->>'source_ref' = 'gate-test-g7-reject'")
        assert cur.fetchone()[0] == 0


def test_g7_unmeasurable_reason_creates_unmeasurable_expectation():
    """Черновик с явной причиной проходит G7 и получает ex_ type='unmeasurable' —
    видимый на витрине, не тишина (акс. критерий тикета)."""
    r = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-g7-ok", "title": "Функциональный покой руки",
        "unmeasurable_reason": "нет метрики, отслеживающей покой конечности",
    })
    body = r.json()
    assert body["accepted"] is True and body["measurable"] is False

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT type, reason FROM {schema()}.expectation WHERE rec_id = %s", (body["id"],))
        ex_type, reason = cur.fetchone()
        assert ex_type == "unmeasurable" and reason == "нет метрики, отслеживающей покой конечности"


def test_g7_rejected_draft_logged_to_issue_log_not_silently_dropped():
    """Отклонённый ворoтами черновик пишется в issue_log, не пропадает молча
    (часть 1 тикета) — issue_log.record_issue импортируется локально внутри
    _log_rejected_draft, патчим по месту реального использования: app.issue_log."""
    from unittest.mock import patch
    with patch("app.issue_log.record_issue") as mock_record:
        r = client.post("/recommendations/propose", json={**BASE, "source_ref": "gate-test-g7-logged"})
        assert r.json()["accepted"] is False
        assert mock_record.called
        args, kwargs = mock_record.call_args
        assert "gate-test-g7-logged" in args[1]
        assert kwargs["source"] == "propose_recommendation"


def test_accepted_recommendation_creates_rc_and_ex_with_priority_and_journal():
    _seed_metric_coverage("rhr")
    r = client.post("/recommendations/propose", json={
        **BASE, "source_ref": "gate-test-full", "metric_key": "rhr", "direction": "down",
        "magnitude": 3.0, "is_bioage_driver": True,
    })
    body = r.json()
    assert body["accepted"] is True and body["measurable"] is True and body["priority"] == "high"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT priority, journal_ref FROM {schema()}.recommendation WHERE id = %s", (body["id"],))
        priority, journal_ref = cur.fetchone()
        assert priority == "high" and journal_ref is not None
        cur.execute(f"SELECT count(*) FROM {schema()}.expectation WHERE rec_id = %s", (body["id"],))
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT op FROM {schema()}.journal WHERE object_id = %s", (body["id"],))
        assert cur.fetchone()[0] == "create"
