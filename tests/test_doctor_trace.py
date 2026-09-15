"""Phase 4 плана нового доктора — trace.py: card.agent_step."""
from app.db import get_conn, schema
from app.doctor import trace
from app.doctor.dialog import write_turn


def _make_turn(chat_id="1"):
    with get_conn() as conn, conn.cursor() as cur:
        turn_id = write_turn(cur, chat_id=chat_id, update_id=1, role="user", text="тест")
        conn.commit()
    return turn_id


def test_write_step_model_role():
    turn_id = _make_turn()
    with get_conn() as conn, conn.cursor() as cur:
        step_id = trace.write_step(cur, turn_id=turn_id, step_no=0, role="model",
                                    model="google/gemini-3.8-flash", latency_ms=1200,
                                    tokens_prompt=500, tokens_completion=80, cost_usd=0.002)
        conn.commit()
    assert step_id.startswith("as_")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT turn_id, step_no, role, model, latency_ms, tokens_prompt, "
                    f"tokens_completion, cost_usd FROM {schema()}.agent_step WHERE id = %s", (step_id,))
        row = cur.fetchone()
    assert row == (turn_id, 0, "model", "google/gemini-3.8-flash", 1200, 500, 80, 0.002)


def test_write_step_tool_role():
    turn_id = _make_turn(chat_id="2")
    with get_conn() as conn, conn.cursor() as cur:
        step_id = trace.write_step(cur, turn_id=turn_id, step_no=1, role="tool",
                                    tool_name="Read_Symptoms", tool_args={"limit": 5},
                                    tool_result_hash="abc123")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT role, tool_name, tool_args, tool_result_hash FROM {schema()}.agent_step "
                    f"WHERE id = %s", (step_id,))
        row = cur.fetchone()
    assert row[0] == "tool"
    assert row[1] == "Read_Symptoms"
    assert row[2] == {"limit": 5}
    assert row[3] == "abc123"


def test_write_step_references_dialog_turn():
    """FK card.agent_step.turn_id -> card.dialog_turn(id) — вставка с несуществующим
    turn_id должна упасть, а не тихо создать осиротевшую строку."""
    import psycopg
    with get_conn() as conn, conn.cursor() as cur:
        try:
            trace.write_step(cur, turn_id="dt_not_real", step_no=0, role="model")
            conn.commit()
            assert False, "ожидалась ошибка внешнего ключа"
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
