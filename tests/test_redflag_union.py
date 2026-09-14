"""П5 (CARD_ARCHITECTURE_PLAN §6, союз A/B/bracelet-cross/C) — уровни, сессии,
запись rf_. Канонический сценарий спеки (F7): пересечение с браслетом = L3
немедленно — проверен на РЕАЛЬНОЙ форме браслетных данных Влада (новокаин),
не абстрактном примере."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import redflag_union
from app.db import get_conn, schema
from app.main import app
from app.redflag_b import LayerBResult, Modality, SeverityFactors

client = TestClient(app)


def _seed_bracelet(metric_key: str, text: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, salience, metric_key, value_text) "
            f"VALUES (%s, now(), '{{}}', 'confirmed', 'bracelet', %s, %s)",
            (f"f_rfb_{metric_key}", metric_key, text),
        )
        conn.commit()


# --- §2.3 уровневая таблица -------------------------------------------------

def test_level_critical_current_is_l3():
    assert redflag_union.level_for("cardiac_acute", {"current": True}) == "L3"


def test_level_critical_past_is_l1():
    assert redflag_union.level_for("cardiac_acute", {"past": True}) == "L1"


def test_level_critical_negation_is_l1_not_none():
    assert redflag_union.level_for("neuro_acute", {"negation": True}) == "L1"


def test_level_critical_third_party_is_l1():
    assert redflag_union.level_for("anaphylaxis", {"third_party": True}) == "L1"


def test_level_high_current_with_factors_is_l3():
    assert redflag_union.level_for("bleeding_gi", {"current": True}, {"duration_min": 20}) == "L3"


def test_level_high_current_without_factors_is_l2():
    assert redflag_union.level_for("bleeding_gi", {"current": True}, {}) == "L2"


def test_level_high_not_current_is_none():
    assert redflag_union.level_for("sepsis_suspect", {"current": False, "past": True}) is None


def test_level_systemic_warning_always_l1_regardless_of_modality():
    assert redflag_union.level_for("systemic_warning", {"current": True}) == "L1"
    assert redflag_union.level_for("systemic_warning", {}) == "L1"


# --- F7: канонический сквозной тест союза, на реальных данных Влада ---------

def test_f7_bracelet_cross_is_immediate_l3_real_novocaine():
    _seed_bracelet("allergy:novocaine_anaphylaxis", "анафилаксия на новокаин")
    result = redflag_union.evaluate_union(
        bracelet_hits=["allergy:novocaine_anaphylaxis"], layer_a_hits=[],
    )
    assert result["level"] == "L3" and result["source"] == "bracelet_cross"


def test_union_layer_a_hit_is_l3():
    result = redflag_union.evaluate_union(
        bracelet_hits=[], layer_a_hits=[{"label": "признаки инсульта", "category": "neuro_acute"}],
    )
    assert result["level"] == "L3" and result["source"] == "A"


def test_union_no_hits_returns_none_level():
    result = redflag_union.evaluate_union(bracelet_hits=[], layer_a_hits=[])
    assert result["level"] is None


def test_union_takes_worst_level_across_sources():
    layer_b = LayerBResult(hit=True, category="bleeding_gi", confidence=0.7,
                            modality=Modality(current=True), severity_factors=SeverityFactors())
    result = redflag_union.evaluate_union(
        bracelet_hits=[], layer_a_hits=[{"label": "x", "category": "systemic_warning"}], layer_b=layer_b,
    )
    # A/systemic_warning -> вообще-то A не бывает systemic_warning в реальности, но
    # тест проверяет чистую механику "берём максимум", не медицинскую правдоподобность.
    levels = [c["level"] for c in result["sources"]]
    assert "L3" in levels and "L2" in levels
    assert result["level"] == "L3"  # худший (наивысший) уровень побеждает


# --- F8: сессии — повтор не дублирует эскалацию -----------------------------

def test_session_reuses_open_session_same_category():
    with get_conn() as conn, conn.cursor() as cur:
        first = redflag_union.record_rf_event(cur, {"level": "L2", "category": "bleeding_gi", "source": "B"})
        second = redflag_union.record_rf_event(cur, {"level": "L1", "category": "bleeding_gi", "source": "B"})
        conn.commit()
    assert first["session_id"] == second["session_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.rf_session WHERE category='bleeding_gi'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.rf_event WHERE session_id=%s", (first["session_id"],))
        assert cur.fetchone()[0] == 2


def test_session_worst_level_never_downgrades():
    with get_conn() as conn, conn.cursor() as cur:
        redflag_union.record_rf_event(cur, {"level": "L3", "category": "cardiac_acute", "source": "A"})
        redflag_union.record_rf_event(cur, {"level": "L1", "category": "cardiac_acute", "source": "B"})
        conn.commit()
        cur.execute(f"SELECT worst_level FROM {schema()}.rf_session WHERE category='cardiac_acute'")
        assert cur.fetchone()[0] == "L3"


def test_different_category_opens_new_session():
    with get_conn() as conn, conn.cursor() as cur:
        a = redflag_union.record_rf_event(cur, {"level": "L1", "category": "bleeding_gi", "source": "B"})
        b = redflag_union.record_rf_event(cur, {"level": "L1", "category": "neuro_acute", "source": "B"})
        conn.commit()
    assert a["session_id"] != b["session_id"]


# --- endpoints ---------------------------------------------------------------

def test_evaluate_endpoint_records_real_bracelet_hit():
    _seed_bracelet("allergy:penicillin", "аллергия на пенициллин")
    r = client.post("/redflag/evaluate", json={"text": "врач хочет колоть пенициллин от инфекции"})
    body = r.json()
    assert body["result"]["level"] == "L3"
    assert body["recorded"]["category"] == "anaphylaxis"


def test_classify_endpoint_mocked_llm():
    fake_response = {
        "hit": True, "category": "cardiac_acute",
        "modality": {"current": True, "past": False, "negation": False, "hypothetical": False, "third_party": False},
        "severity_factors": {"duration_min": 15, "intensity": "high", "combination": [], "progression": None},
        "context_note": "давит в груди 15 минут", "confidence": 0.85,
    }
    with patch("app.redflag_b.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: {"choices": [{"message": {"content": __import__("json").dumps(fake_response)}}]}
        r = client.post("/redflag/classify", json={"text": "давит в груди уже 15 минут"})
    body = r.json()
    assert body["hit"] is True and body["category"] == "cardiac_acute" and body["confidence"] == 0.85


def test_layer_c_endpoint_smoke_no_data_returns_empty():
    r = client.get("/redflag/layer-c")
    assert r.status_code == 200 and r.json() == []
