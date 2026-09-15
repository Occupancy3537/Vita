"""Phase 2 плана нового доктора — gate.py: A+bracelet решают L3 без сети и без
задержки, слой B дописывает ту же сессию после. Приёмка (план §4, шаг 2):
41-примерный корпус зелёный, "новокаин" -> L3 за <200мс без вызова LLM,
card.rf_event перестаёт быть пустым."""
import time

from app.db import get_conn, schema
from app.doctor import gate
from app.redflag_b import LayerBResult
from tests.test_redflag import CRISIS, EDGE, EMERG, SAFE


def _fast_level(text: str) -> str | None:
    with get_conn() as conn, conn.cursor() as cur:
        result = gate.fast_gate(cur, text)
        conn.commit()
    return result["result"].get("level")


def test_emergency_corpus_all_l3():
    fails = [t for t in EMERG if _fast_level(t) != "L3"]
    assert not fails, f"не дали L3: {fails}"


def test_crisis_corpus_all_l3():
    fails = [t for t in CRISIS if _fast_level(t) != "L3"]
    assert not fails, f"не дали L3: {fails}"


def test_safe_corpus_no_level():
    fails = [t for t in SAFE if _fast_level(t) is not None]
    assert not fails, f"ложные срабатывания: {fails}"


def test_edge_corpus_matches_expected_hit():
    fails = []
    for t, exp in EDGE:
        lvl = _fast_level(t)
        if (lvl == "L3") != exp:
            fails.append((t, exp, lvl))
    assert not fails, f"расхождение: {fails}"


def test_novocaine_bracelet_is_l3_under_200ms_no_llm_needed():
    """F7, канонический сквозной тест союза — настоящая браслетная анафилаксия."""
    with get_conn() as conn, conn.cursor() as cur:
        start = time.monotonic()
        result = gate.fast_gate(cur, "делали укол с новокаином, теперь тяжело дышать")
        elapsed_ms = (time.monotonic() - start) * 1000
        conn.commit()
    assert result["result"]["level"] == "L3"
    assert result["result"]["category"] == "anaphylaxis"
    assert elapsed_ms < 200, f"{elapsed_ms}мс — не уложились в бюджет без сети"


def test_fast_gate_records_rf_event_for_hit():
    with get_conn() as conn, conn.cursor() as cur:
        gate.fast_gate(cur, "новокаин, тяжело дышать")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM " + schema() + ".rf_event WHERE category = 'anaphylaxis'")
        assert cur.fetchone()[0] == 1


def test_fast_gate_no_record_for_safe_text():
    with get_conn() as conn, conn.cursor() as cur:
        gate.fast_gate(cur, "лёгкое покалывание в пальцах после того как отлежал руку")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM " + schema() + ".rf_event")
        assert cur.fetchone()[0] == 0


def test_handle_emergency_writes_episode_and_assistant_turn():
    text = "грудь давит, отдаёт в левую руку, одышка"
    with get_conn() as conn, conn.cursor() as cur:
        gate_result = gate.fast_gate(cur, text)
        assert gate_result["result"]["level"] == "L3"
        reply = gate.handle_emergency(cur, "12345", gate_result, text, None)
        conn.commit()
    assert "скорую" in reply.lower()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT symptom_key, status FROM " + schema() + ".episode WHERE symptom_key LIKE 'emergency:%'")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "emergency:cardiac_acute"
        assert rows[0][1] == "open"

        cur.execute("SELECT role, rf_level, wrote_anything, text FROM " + schema() + ".dialog_turn WHERE chat_id = '12345'")
        turns = cur.fetchall()
        assert len(turns) == 1
        assert turns[0][0] == "assistant"
        assert turns[0][1] == "L3"
        assert turns[0][2] is True
        assert turns[0][3] == reply


def test_slow_gate_followup_records_layer_b_hit(monkeypatch):
    fake_result = LayerBResult(hit=True, category="cardiac_acute", degraded=False,
                                confidence=0.9, context_note="test")
    fake_result.modality.current = True

    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: fake_result)

    gate.slow_gate_followup("любой текст — B замокан")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM " + schema() + ".rf_event WHERE source = 'B'")
        assert cur.fetchone()[0] == 1


def test_slow_gate_followup_no_op_when_b_does_not_hit(monkeypatch):
    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: LayerBResult(hit=False))

    gate.slow_gate_followup("текст без флагов")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM " + schema() + ".rf_event")
        assert cur.fetchone()[0] == 0


def test_slow_gate_followup_no_op_when_b_degraded(monkeypatch):
    monkeypatch.setattr(gate, "classify_layer_b",
                         lambda text, prior_replies=None: LayerBResult(hit=True, category="cardiac_acute", degraded=True))

    gate.slow_gate_followup("текст, где B деградировал")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM " + schema() + ".rf_event")
        assert cur.fetchone()[0] == 0


def test_slow_gate_followup_survives_classify_exception(monkeypatch):
    def boom(text, prior_replies=None):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(gate, "classify_layer_b", boom)
    gate.slow_gate_followup("не должно уронить фоновую задачу")  # не бросает исключение наружу
