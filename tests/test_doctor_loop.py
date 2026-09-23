"""Phase 4 плана нового доктора — loop.py: агентный цикл. OpenRouter мокается
(app.doctor.loop._call_model) — юнит-тесты не бьют по сети/деньгам, живой сквозной
прогон уже сделан вручную при разработке (см. STATE.md)."""
import time

import pytest

from app.db import get_conn
from app.doctor import config, loop
from app.doctor.contract import StagedWrite, TurnResult
from app.doctor.dialog import write_turn


def _real_turn(chat_id="1", update_id=1) -> str:
    """card.agent_step.turn_id имеет FK на dialog_turn(id) (в т.ч. в card_test,
    см. migrate_doctor_tables.sql) — в реальном потоке turn_id всегда приходит
    из intake.py, который вставляет user-ход ДО вызова run_turn; здесь то же
    самое, а не произвольная строка."""
    with get_conn() as conn, conn.cursor() as cur:
        turn_id = write_turn(cur, chat_id=chat_id, update_id=update_id, role="user", text="тест")
        conn.commit()
    return turn_id


def _usage(prompt=10, completion=20, reasoning=0, cost=0.001):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning}, "cost": cost}


def _final_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": None}}],
            "usage": _usage()}


def _tool_call_response(name, args, call_id="call_1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}},
    ]}}], "usage": _usage()}


@pytest.fixture(autouse=True)
def fast_deadline(monkeypatch):
    monkeypatch.setattr(config, "TURN_DEADLINE_SECONDS", 60.0)
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 3)


@pytest.fixture(autouse=True)
def no_real_telegram_calls(monkeypatch):
    """2026-09-18: run_turn теперь держит фоновый keepalive-поток на
    send_chat_action (индикатор "печатает" на весь ход, не один раз в начале) —
    в быстрых тестах поток обычно не успевает ни разу сработать до stop_event
    (см. _typing_keepalive), но полагаться на тайминг для "не бьёт по сети" —
    хрупко. Мокаем явно, тот же принцип, что и у _call_model выше в файле."""
    monkeypatch.setattr(loop.telegram, "send_chat_action", lambda *a, **k: None)


def test_run_turn_no_tool_calls_returns_final_text(monkeypatch):
    monkeypatch.setattr(loop, "_call_model", lambda messages, model, timeout: _final_response("Обычный ответ"))
    r = loop.run_turn(chat_id="1", person_id="self", text="как дела", turn_id=_real_turn())
    assert isinstance(r, TurnResult)
    assert r.reply_text == "Обычный ответ"
    assert r.staged_writes == []
    assert r.wrote_anything is False


def test_run_turn_empty_content_falls_back_to_degraded(monkeypatch):
    monkeypatch.setattr(loop, "_call_model", lambda messages, model, timeout: _final_response(""))
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=_real_turn())
    assert r.reply_text == loop.DEGRADED_REPLY


def test_run_turn_model_exception_degrades_gracefully(monkeypatch):
    def boom(messages, model, timeout):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(loop, "_call_model", boom)
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=_real_turn())
    assert r.reply_text == loop.DEGRADED_REPLY


def test_run_turn_executes_read_tool_then_final_text(monkeypatch):
    calls = {"n": 0}

    def fake_call_model(messages, model, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call_response("Read_Symptoms", "{}")
        # вторая итерация — модель видит результат инструмента и отвечает текстом
        assert any(m.get("role") == "tool" for m in messages)
        return _final_response("Разбор с учётом истории симптомов")

    monkeypatch.setattr(loop, "_call_model", fake_call_model)
    r = loop.run_turn(chat_id="1", person_id="self", text="снова болит", turn_id=_real_turn())
    assert calls["n"] == 2
    assert r.reply_text == "Разбор с учётом истории симптомов"


def test_run_turn_write_tool_call_is_staged_not_committed(monkeypatch):
    calls = {"n": 0}

    def fake_call_model(messages, model, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call_response("Record_Symptom",
                                        '{"symptom_id": "sid1", "symptom": "боль в спине"}')
        return _final_response("Записал и разобрал")

    monkeypatch.setattr(loop, "_call_model", fake_call_model)
    r = loop.run_turn(chat_id="1", person_id="self", text="болит спина", turn_id=_real_turn())
    assert r.wrote_anything is True
    assert len(r.staged_writes) == 1
    assert r.staged_writes[0].kind == "symptom"
    assert r.staged_writes[0].payload["symptom_id"] == "sid1"

    # commit.py ещё нет (Phase 5) — health.symptom_log НЕ должен получить новую
    # строку с этим symptom_id только от одного вызова run_turn.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM health.symptom_log WHERE symptom_id = 'sid1'")
        assert cur.fetchone()[0] == 0


def test_run_turn_invalid_write_tool_args_not_staged(monkeypatch):
    calls = {"n": 0}

    def fake_call_model(messages, model, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call_response("Record_Symptom", '{"symptom_id": "sid1"}')  # symptom обязателен
        return _final_response("ответ")

    monkeypatch.setattr(loop, "_call_model", fake_call_model)
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=_real_turn())
    assert r.staged_writes == []
    assert r.wrote_anything is False


def test_run_turn_exhausts_rounds_degrades(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(loop, "_call_model",
                         lambda messages, model, timeout: _tool_call_response("Read_Symptoms", "{}"))
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=_real_turn())
    assert "не успел" in r.reply_text.lower()
    # L2 (аудит логики, 2026-09-23): деградация без ЕДИНОГО намёка на срочность
    # была худшим сценарием для сообщения, которое могло быть кризисным, а
    # модель просто не успела его оценить — теперь безопасный хвост есть всегда.
    assert "103" in r.reply_text and "8-800-2000-122" in r.reply_text


def test_run_turn_deadline_exceeded_degrades(monkeypatch):
    monkeypatch.setattr(config, "TURN_DEADLINE_SECONDS", 0.0)
    called = {"n": 0}
    monkeypatch.setattr(loop, "_call_model", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _final_response("x"))
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=_real_turn())
    assert called["n"] == 0  # дедлайн уже истёк до первого вызова модели
    assert "срок" in r.reply_text.lower()
    assert "103" in r.reply_text and "8-800-2000-122" in r.reply_text


def test_run_turn_writes_agent_step_traces(monkeypatch):
    monkeypatch.setattr(loop, "_call_model", lambda messages, model, timeout: _final_response("ответ"))
    turn_id = _real_turn()
    r = loop.run_turn(chat_id="1", person_id="self", text="тест", turn_id=turn_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT role, model, tokens_prompt, tokens_completion, cost_usd "
                     "FROM card_test.agent_step WHERE turn_id = %s", (turn_id,))
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "model"
    assert rows[0][2] == 10
    assert rows[0][3] == 20


def test_run_tools_parallel_respects_per_tool_timeout(monkeypatch):
    def slow_read_symptoms(cur, args):
        time.sleep(0.3)
        return {"ok": True}

    monkeypatch.setitem(loop.TOOLS_BY_NAME, "Read_Symptoms",
                         {**loop.TOOLS_BY_NAME["Read_Symptoms"], "executor": slow_read_symptoms, "timeout": 0.05})
    results = loop._run_tools_parallel([{"id": "c1", "name": "Read_Symptoms", "arguments": {}}])
    assert results["c1"] == {"error": "tool_timeout"}


def test_run_tool_unknown_name_returns_error():
    assert loop._run_tool("Not_A_Real_Tool", {}) == {"error": "unknown_tool: Not_A_Real_Tool"}
