"""app/llm_usage.py — учёт стоимости LLM-вызовов вне доктора (2026-09-22):
«полные расходы на ИИ» на странице «Настройки». Доктор пишет свой трейс в
card.agent_step — сюда не дублируется."""
from app import llm_usage
from app.db import get_conn, schema


def _rows():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT module, model, tokens_prompt, tokens_completion, cost_usd "
                    f"FROM {schema()}.llm_usage ORDER BY id")
        return cur.fetchall()


def test_record_writes_row():
    llm_usage.record("test_module", "test/model",
                     {"cost": 0.0123, "prompt_tokens": 100, "completion_tokens": 20})
    rows = _rows()
    assert len(rows) == 1
    module, model, tp, tc, cost = rows[0]
    assert module == "test_module" and model == "test/model"
    assert tp == 100 and tc == 20 and float(cost) == 0.0123


def test_record_skips_when_usage_absent_or_empty():
    """Часть провайдеров не возвращает usage — писать нечего, и не пишем."""
    llm_usage.record("test_module", "m", None)
    llm_usage.record("test_module", "m", {})
    assert _rows() == []


def test_record_never_raises_on_db_failure(monkeypatch):
    """Учёт денег не должен ронять сам LLM-вызов (fail-safe, как run_log)."""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_usage, "get_conn", boom)
    llm_usage.record("test_module", "m", {"cost": 0.1})  # не бросает
