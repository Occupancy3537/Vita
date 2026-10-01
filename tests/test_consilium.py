"""Консилиум специалистов (2026-09-25) — app/consilium.py.

Урок провала 23.09 (см. докстринг модуля): короткий список действий, не
литературный обзор; «Перспективное» — грейд ИЗ card.publication (структурно),
не из свободного текста модели. LLM (httpx.post) и propose_recommendation
мокаются — юниты не платят и не создают реальные рекомендации; персистентность
(card.opinion/disagreement/consilium_report) проверяется против реальной
изолированной card_test (см. tests/conftest.py — CARD_PG_SCHEMA=card_test)."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from ulid import ULID

from app import consilium as cs
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _chat_completion(content: dict, cost: float = 0.01) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 50}}


# ─────── _call_llm — сеть мокается, стоимость копится в _cost_tracker ───────

def test_call_llm_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cs._call_llm("sys", "user", "mod") == {}


def test_call_llm_parses_json_and_tracks_cost(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cs._reset_cost_tracker()

    def fake_post(url, headers=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer test-key"
        return httpx.Response(request=httpx.Request("POST", url), status_code=200,
                              json=_chat_completion({"foo": "bar"}, cost=0.05))
    monkeypatch.setattr(cs.httpx, "post", fake_post)

    result = cs._call_llm("sys", "user", "consilium_test")
    assert result == {"foo": "bar"}
    assert cs._current_run_cost() == pytest.approx(0.05)


def test_call_llm_web_search_adds_plugin(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return httpx.Response(request=httpx.Request("POST", url), status_code=200,
                              json=_chat_completion({}))
    monkeypatch.setattr(cs.httpx, "post", fake_post)

    cs._call_llm("sys", "user", "consilium_test", web_search=True)
    assert captured["body"]["plugins"] == [{"id": "web", "max_results": cs.WEB_SEARCH_MAX_RESULTS}]


def test_call_llm_no_web_search_by_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return httpx.Response(request=httpx.Request("POST", url), status_code=200,
                              json=_chat_completion({}))
    monkeypatch.setattr(cs.httpx, "post", fake_post)

    cs._call_llm("sys", "user", "consilium_test")
    assert "plugins" not in captured["body"]


def test_call_llm_honest_empty_on_network_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def boom(*a, **kw):
        raise httpx.ConnectError("нет связи")
    monkeypatch.setattr(cs.httpx, "post", boom)

    assert cs._call_llm("sys", "user", "consilium_test") == {}


def test_call_llm_honest_empty_on_malformed_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(request=httpx.Request("POST", url), status_code=200,
                              json={"choices": [{"message": {"content": "не json"}}]})
    monkeypatch.setattr(cs.httpx, "post", fake_post)

    assert cs._call_llm("sys", "user", "consilium_test") == {}


# ─────── select_specialists — свободный подбор, но с честным fallback ───────

def test_select_specialists_uses_llm_choice(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"specialists": ["невролог", "ортопед"]})
    assert cs.select_specialists("боль в боку", None) == ["невролог", "ортопед"]


def test_select_specialists_pads_when_llm_gives_only_one(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"specialists": ["кардиолог"]})
    result = cs.select_specialists("липиды", None)
    assert len(result) >= cs.MIN_SPECIALISTS
    assert "кардиолог" in result


def test_select_specialists_truncates_to_max(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"specialists": ["a", "b", "c", "d", "e"]})
    assert len(cs.select_specialists("тема", None)) == cs.MAX_SPECIALISTS


def test_select_specialists_honest_fallback_when_llm_empty(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {})
    result = cs.select_specialists("тема", None)
    assert len(result) >= cs.MIN_SPECIALISTS
    assert all(isinstance(s, str) and s for s in result)


# ─────── run_specialist / run_skeptic / synthesize — defaults & shape ───────

def test_run_specialist_sets_role_and_defaults(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"claim": "мнение"})
    result = cs.run_specialist("невролог", {}, "тема", None)
    assert result["role"] == "невролог"
    assert result["claim"] == "мнение"
    assert result["established_actions"] == []
    assert result["emerging"] == []
    assert result["evidence_refs"] == []


def test_run_specialist_uses_web_search(monkeypatch):
    captured = {}

    def fake_call_llm(system, user, module, **kw):
        captured.update(kw)
        return {}
    monkeypatch.setattr(cs, "_call_llm", fake_call_llm)
    cs.run_specialist("невролог", {}, "тема", None)
    assert captured.get("web_search") is True


def test_run_skeptic_defaults_when_llm_empty(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {})
    result = cs.run_skeptic([], "тема")
    assert result == {"remarks": [], "top_concerns": []}


def test_synthesize_truncates_actions_to_max(monkeypatch):
    actions = [{"imperative": f"действие {i}"} for i in range(cs.MAX_ACTIONS + 3)]
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"actions": actions})
    result = cs.synthesize([], {}, "тема", None)
    assert len(result["actions"]) == cs.MAX_ACTIONS


def test_synthesize_empty_actions_is_legitimate(monkeypatch):
    """Урок 23.09: пустой список действий — не баг, а честный "менять нечего"."""
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"actions": []})
    result = cs.synthesize([], {}, "тема", None)
    assert result["actions"] == []


# ─────── _grade_from_publication / _grade_emerging — грейд ИЗ базы, не из модели ───────

def test_grade_from_publication_found_meta_analysis_with_phase():
    pubs = {"pub_1": {"design_type": "trial", "phase": "фаза 2"}}
    grade, basis = cs._grade_from_publication("pub_1", pubs)
    assert grade == "испытание (фаза 2)"
    assert basis == "publication"


def test_grade_from_publication_found_without_phase():
    pubs = {"pub_1": {"design_type": "meta-analysis", "phase": None}}
    grade, basis = cs._grade_from_publication("pub_1", pubs)
    assert grade == "мета-анализ"
    assert basis == "publication"


def test_grade_from_publication_unknown_id_is_honest_none():
    grade, basis = cs._grade_from_publication("pub_does_not_exist", {"pub_1": {}})
    assert grade is None
    assert basis == "не подтверждено базой публикаций"


def test_grade_from_publication_no_source_is_honest_none():
    grade, basis = cs._grade_from_publication(None, {"pub_1": {}})
    assert grade is None
    assert basis == "не подтверждено базой публикаций"


def test_grade_emerging_never_uses_model_maturity_as_grade():
    """Приёмка тикета буквально: "«Перспективное» отделено, грейды из
    публикаций, не из мнения модели" — modela maturity никогда не попадает
    в поле grade, только в исходное поле maturity (для контекста)."""
    emerging = [{"method": "рапамицин", "maturity": "модель сказала: почти готово", "source": "pub_1"}]
    pubs = {"pub_1": {"design_type": "rct", "phase": None}}
    graded = cs._grade_emerging(emerging, pubs)
    assert graded[0]["grade"] == "РКИ"
    assert graded[0]["maturity"] == "модель сказала: почти готово"  # сохранено, но не используется как грейд


def test_grade_emerging_unmatched_source_stays_ungraded():
    emerging = [{"method": "неизвестный метод", "maturity": "модель уверена", "source": None}]
    graded = cs._grade_emerging(emerging, {})
    assert graded[0]["grade"] is None
    assert graded[0]["grade_basis"] == "не подтверждено базой публикаций"


# ─────── build_compact_summary / build_full_document — короткий список, не простыня ───────

def test_build_compact_summary_empty_actions_says_nothing_to_change():
    text = cs.build_compact_summary("тема", [], [], [], [])
    assert "менять нечего" in text


def test_build_compact_summary_accepted_and_rejected_tags():
    a1 = {"imperative": "сделать МРТ"}
    r1 = {"accepted": True}
    a2 = {"imperative": "начать препарат X"}
    r2 = {"accepted": False, "rejected_gate": "G7"}
    text = cs.build_compact_summary("тема", [(a1, r1), (a2, r2)], [], [], [])
    assert "✅ сделать МРТ" in text
    assert "⛔ начать препарат X" in text


def test_build_compact_summary_shows_emerging_grade_not_model_maturity():
    emerging = [{"method": "сенолитики", "maturity": "модель считает многообещающим", "grade": "испытание (фаза 1)"}]
    text = cs.build_compact_summary("тема", [], emerging, [], [])
    assert "испытание (фаза 1)" in text


def test_build_full_document_has_all_blocks():
    a1 = {"imperative": "сделать МРТ", "rationale": "боль 10 лет без диагноза"}
    r1 = {"accepted": True}
    specialist_opinions = [{"role": "невролог", "claim": "нужна визуализация", "confidence": 0.7, "evidence_refs": ["lab_1"]}]
    skeptic_review = {"top_concerns": ["мало данных для однозначного вывода"]}
    synthesis = {
        "emerging": [{"method": "рапамицин", "maturity": "фаза 2", "grade": "испытание (фаза 2)",
                      "what_is_needed": "завершение испытаний", "source": "pub_1"}],
        "disagreements": [{"between": ["невролог", "ортопед"], "about": "источник боли", "resolving_test": "МРТ"}],
    }
    text = cs.build_full_document("боль в боку", "10 лет без диагноза", specialist_opinions, skeptic_review,
                                  synthesis, [(a1, r1)])
    assert "## Действия" in text
    assert "## Перспективное (не внедрено)" in text
    assert "## Разногласия" in text
    assert "## Замечания скептика" in text
    assert "## Мнения специалистов (полностью)" in text
    assert "испытание (фаза 2)" in text
    assert "рекомендация создана" in text


def test_build_full_document_empty_actions_says_nothing_to_change():
    text = cs.build_full_document("тема", None, [], {}, {}, [])
    assert "Менять нечего" in text


def test_build_full_document_shows_gate_rejection_reason():
    a1 = {"imperative": "начать препарат X", "rationale": "..."}
    r1 = {"accepted": False, "rejected_gate": "G7", "rejected_reason": "нет ожидания"}
    text = cs.build_full_document("тема", None, [], {}, {}, [(a1, r1)])
    assert "отклонено воротами (G7: нет ожидания)" in text


# ─────── persistence — card.opinion / card.disagreement (реальная card_test) ───────

def _cleanup(cur, table, ids):
    for i in ids:
        cur.execute(sql_delete(table), (i,))


def sql_delete(table):
    from psycopg import sql
    return sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), table))


def test_write_opinion_persists_and_journals():
    report_id = f"cs_{ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        op_id = cs._write_opinion(cur, report_id, "невролог", "нужна визуализация", None, ["lab_1"], 0.8)
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("SELECT author, claim, report_id, confidence FROM {t} WHERE id = %s")
                       .format(t=sql.Identifier(schema(), "opinion")), (op_id,))
            row = cur.fetchone()
        assert row == ("невролог", "нужна визуализация", report_id, 0.8)
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            _cleanup(cur, "opinion", [op_id])
            conn.commit()


def test_write_disagreement_persists_with_repurposed_columns():
    report_id = f"cs_{ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        dis_id = cs._write_disagreement(cur, report_id, ["невролог", "ортопед"], "источник боли", "МРТ")
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("SELECT opinion_doctor, opinion_advisor, significance, class, report_id FROM {t} "
                                "WHERE id = %s").format(t=sql.Identifier(schema(), "disagreement")), (dis_id,))
            row = cur.fetchone()
        assert row[0] == "невролог"
        assert row[1] == "ортопед"
        assert "источник боли" in row[2] and "МРТ" in row[2]
        assert row[3] == "consilium_specialist_disagreement"
        assert row[4] == report_id
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            _cleanup(cur, "disagreement", [dis_id])
            conn.commit()


# ─────── _propose_action_as_recommendation — G7 через существующий propose_recommendation ───────

def test_propose_action_accepted_links_publication_id(monkeypatch):
    from app import recommendations as rc

    captured = []

    class FakeResp:
        def model_dump(self):
            return {"accepted": True, "id": "rc_test1", "measurable": True, "priority": "normal",
                    "duplicate_of": None, "rejected_gate": None, "rejected_reason": None}

    def fake_propose(req):
        captured.append(req)
        return FakeResp()
    monkeypatch.setattr(rc, "propose_recommendation", fake_propose)

    action = {"imperative": "сделать МРТ", "rationale": "боль 10 лет", "metric_key": "pain_score",
              "direction": "down", "magnitude": 2, "window_days": 30, "expectation_type": "delta_abs",
              "evidence_refs": ["pub_123"]}
    result = cs._propose_action_as_recommendation(action, "consilium:cs_1:0")

    assert result["accepted"] is True
    assert result["id"] == "rc_test1"
    assert captured[0].publication_id == "pub_123"
    assert captured[0].kind == "consilium"
    assert captured[0].origin == "consilium"


def test_propose_action_rejected_by_gate_returns_honest_reason(monkeypatch):
    from app import recommendations as rc

    class FakeResp:
        def model_dump(self):
            return {"accepted": False, "id": None, "measurable": None, "priority": None,
                    "duplicate_of": None, "rejected_gate": "G7", "rejected_reason": "нет ожидания"}

    monkeypatch.setattr(rc, "propose_recommendation", lambda req: FakeResp())

    action = {"imperative": "начать препарат X", "rationale": "..."}
    result = cs._propose_action_as_recommendation(action, "consilium:cs_1:0")
    assert result["accepted"] is False
    assert result["rejected_gate"] == "G7"


def test_propose_action_unmeasurable_reason_passed_through(monkeypatch):
    from app import recommendations as rc
    captured = []

    class FakeResp:
        def model_dump(self):
            return {"accepted": True, "id": "rc_test2", "measurable": False, "priority": "normal",
                    "duplicate_of": None, "rejected_gate": None, "rejected_reason": None}

    def fake_propose(req):
        captured.append(req)
        return FakeResp()
    monkeypatch.setattr(rc, "propose_recommendation", fake_propose)

    action = {"imperative": "обсудить с врачом дозировку", "rationale": "...",
              "unmeasurable_reason": "решение принимает врач очно"}
    cs._propose_action_as_recommendation(action, "consilium:cs_1:0")
    assert captured[0].metric_key is None
    assert captured[0].unmeasurable_reason == "решение принимает врач очно"


# ─────── run_consilium — сквозной прогон, всё внешнее замокано ───────

def _fake_specialist(role, context, topic, question):
    return {"role": role, "claim": f"{role}: мнение", "established_actions": [],
            "emerging": [], "evidence_refs": [], "confidence": 0.6}


def test_run_consilium_end_to_end_writes_opinions_and_report(monkeypatch):
    monkeypatch.setattr(cs, "gather_context", lambda cur, topic: {"relevant_publications": []})
    monkeypatch.setattr(cs, "select_specialists", lambda topic, question: ["невролог", "ортопед"])
    monkeypatch.setattr(cs, "run_specialist", _fake_specialist)
    monkeypatch.setattr(cs, "run_skeptic", lambda opinions, topic: {"remarks": [], "top_concerns": ["мало данных"]})
    monkeypatch.setattr(cs, "synthesize", lambda *a, **kw: {
        "actions": [{"imperative": "сделать МРТ", "rationale": "боль без диагноза",
                    "unmeasurable_reason": "диагностика, не измеримый эффект", "evidence_refs": []}],
        "emerging": [], "disagreements": [{"between": ["невролог", "ортопед"], "about": "источник боли",
                                            "resolving_test": "МРТ"}],
    })

    class FakeResp:
        def model_dump(self):
            return {"accepted": True, "id": "rc_e2e", "measurable": False, "priority": "normal",
                    "duplicate_of": None, "rejected_gate": None, "rejected_reason": None}
    from app import recommendations as rc
    monkeypatch.setattr(rc, "propose_recommendation", lambda req: FakeResp())

    monkeypatch.setattr(cs, "family_verdict", lambda *a, **kw: {
        "verdict": "сделать МРТ в течение двух недель", "short_actions": ["сделать МРТ"]})
    result = cs.run_consilium("боль в боку 10 лет, причина не найдена", trigger="command")
    report_id = result["report_id"]
    try:
        assert result["status"] == "completed"
        assert "Врачи посовещались и решили" in result["compact_summary"]
        assert "сделать МРТ в течение двух недель" in result["compact_summary"]
        assert "## Действия" in result["full_text"]

        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE report_id = %s")
                       .format(t=sql.Identifier(schema(), "opinion")), (report_id,))
            n_opinions = cur.fetchone()[0]
            cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE report_id = %s")
                       .format(t=sql.Identifier(schema(), "disagreement")), (report_id,))
            n_disagreements = cur.fetchone()[0]
            cur.execute(sql.SQL("SELECT status, cost_usd FROM {t} WHERE id = %s")
                       .format(t=sql.Identifier(schema(), "consilium_report")), (report_id,))
            report_row = cur.fetchone()
        # 2 специалиста + обязательный скептик = 3 мнения (Часть 1.4)
        assert n_opinions == 3
        assert n_disagreements == 1
        assert report_row[0] == "completed"
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("DELETE FROM {t} WHERE report_id = %s").format(t=sql.Identifier(schema(), "opinion")), (report_id,))
            cur.execute(sql.SQL("DELETE FROM {t} WHERE report_id = %s").format(t=sql.Identifier(schema(), "disagreement")), (report_id,))
            cur.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "consilium_report")), (report_id,))
            conn.commit()


def test_run_consilium_empty_result_is_legitimate(monkeypatch):
    """Урок 23.09: "менять нечего" — валидный итог, не ошибка."""
    monkeypatch.setattr(cs, "gather_context", lambda cur, topic: {"relevant_publications": []})
    monkeypatch.setattr(cs, "select_specialists", lambda topic, question: ["терапевт превентивной медицины"])
    monkeypatch.setattr(cs, "run_specialist", _fake_specialist)
    monkeypatch.setattr(cs, "run_skeptic", lambda opinions, topic: {"remarks": [], "top_concerns": []})
    monkeypatch.setattr(cs, "synthesize", lambda *a, **kw: {"actions": [], "emerging": [], "disagreements": []})

    result = cs.run_consilium("общий профиль долголетия", trigger="monthly")
    report_id = result["report_id"]
    try:
        assert result["status"] == "empty"
        assert "Менять ничего не нужно" in result["compact_summary"]
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("DELETE FROM {t} WHERE report_id = %s").format(t=sql.Identifier(schema(), "opinion")), (report_id,))
            cur.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "consilium_report")), (report_id,))
            conn.commit()


def test_run_consilium_specialist_failure_does_not_crash_whole_run(monkeypatch):
    """"Одна упавшая роль не должна ронять весь консилиум" (докстринг run_consilium)."""
    monkeypatch.setattr(cs, "gather_context", lambda cur, topic: {"relevant_publications": []})
    monkeypatch.setattr(cs, "select_specialists", lambda topic, question: ["невролог", "ортопед"])

    def flaky_specialist(role, context, topic, question):
        if role == "невролог":
            raise RuntimeError("модель недоступна")
        return _fake_specialist(role, context, topic, question)
    monkeypatch.setattr(cs, "run_specialist", flaky_specialist)
    monkeypatch.setattr(cs, "run_skeptic", lambda opinions, topic: {"remarks": [], "top_concerns": []})
    monkeypatch.setattr(cs, "synthesize", lambda *a, **kw: {"actions": [], "emerging": [], "disagreements": []})

    result = cs.run_consilium("тема", trigger="command")
    report_id = result["report_id"]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE report_id = %s")
                       .format(t=sql.Identifier(schema(), "opinion")), (report_id,))
            n_opinions = cur.fetchone()[0]
        # только "ортопед" пережил + обязательный скептик = 2
        assert n_opinions == 2
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("DELETE FROM {t} WHERE report_id = %s").format(t=sql.Identifier(schema(), "opinion")), (report_id,))
            cur.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "consilium_report")), (report_id,))
            conn.commit()


# ─────── run_monthly / run_scheduler — расписание + догоняющий тик ───────

def test_run_monthly_uses_general_profile_topic_and_notifies(monkeypatch):
    captured = {}

    def fake_run_consilium(topic, question=None, trigger="command"):
        captured["topic"] = topic
        captured["trigger"] = trigger
        return {"report_id": "cs_x", "compact_summary": "итог", "full_text": "...", "status": "completed"}
    monkeypatch.setattr(cs, "run_consilium", fake_run_consilium)
    monkeypatch.setattr(cs.notify, "notify", lambda source, priority, text: captured.setdefault("notified", (source, priority, text)))

    cs.run_monthly()
    assert captured["topic"] == "общий профиль долголетия"
    assert captured["trigger"] == "monthly"
    assert captured["notified"][0] == "consilium_monthly"


def test_run_scheduler_catches_up_when_last_run_stale(monkeypatch):
    calls = []
    monkeypatch.setattr(cs.run_log, "last_ok_at", lambda name: datetime.now(timezone.utc) - timedelta(days=40))
    monkeypatch.setattr(cs, "run_monthly", lambda: calls.append("ran") or {})
    monkeypatch.setattr(cs.run_log, "mark_run", lambda name: calls.append("marked"))

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        cs.run_scheduler()
    assert calls == ["ran", "marked"]


def test_run_scheduler_no_catchup_when_recent(monkeypatch):
    calls = []
    monkeypatch.setattr(cs.run_log, "last_ok_at", lambda name: datetime.now(timezone.utc))
    monkeypatch.setattr(cs, "run_monthly", lambda: calls.append("ran") or {})

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        cs.run_scheduler()
    assert calls == []


def test_run_scheduler_catches_up_when_never_run(monkeypatch):
    calls = []
    monkeypatch.setattr(cs.run_log, "last_ok_at", lambda name: None)
    monkeypatch.setattr(cs, "run_monthly", lambda: calls.append("ran") or {})
    monkeypatch.setattr(cs.run_log, "mark_run", lambda name: None)

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        cs.run_scheduler()
    assert calls == ["ran"]


# ─────── get_consilium_reports — дашборд-секция (Часть 4.1, отдельная от recommendations_log) ───────

def test_get_consilium_reports_returns_recent_reports():
    report_id = f"cs_{ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        from psycopg import sql
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, topic, question, trigger, roles, actions, emerging, skeptic_notes, "
                    "full_text, status, cost_usd) VALUES (%s, %s, %s, 'command', '[]', '[]', '[]', '[]', %s, "
                    "'completed', 0.01)")
            .format(t=sql.Identifier(schema(), "consilium_report")),
            (report_id, "боль в боку", None, "полный документ"),
        )
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            result = cs.get_consilium_reports(cur, limit=5)
        assert any(r["id"] == report_id for r in result["reports"])
        found = next(r for r in result["reports"] if r["id"] == report_id)
        assert found["topic"] == "боль в боку"
        assert found["status"] == "completed"
        assert found["full_text"] == "полный документ"
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "consilium_report")), (report_id,))
            conn.commit()


# ─────── _relevant_publications — id обязателен (грейд считается по нему) ───────

def test_relevant_publications_includes_id_field():
    pub_id = f"pub_{ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        from psycopg import sql
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, ts_event, provenance, source, title, design_type, phase) "
                    "VALUES (%s, now(), %s, 'pubmed', %s, 'rct', NULL)")
            .format(t=sql.Identifier(schema(), "publication")),
            (pub_id, json.dumps({"source_ref": f"doi:test-{pub_id}"}), "Rapamycin trial for longevity"),
        )
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            pubs = cs._relevant_publications(cur, "rapamycin", limit=5)
        assert any(p["id"] == pub_id for p in pubs)
        found = next(p for p in pubs if p["id"] == pub_id)
        assert found["design_type"] == "rct"
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            from psycopg import sql
            cur.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "publication")), (pub_id,))
            conn.commit()


# ─────── итог семейного врача (2026-10-01): «врачи посовещались и решили …» ───────

def test_family_verdict_parses_and_aligns_short_actions(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {
        "verdict": "Жиры менее 28 г в день; псиллиум 1 ч. л. в 12:00; через месяц кровь на холестерин.",
        "short_actions": ["Жиры < 28 г/день", "Псиллиум 1 ч. л. в 12:00"]})
    out = cs.family_verdict("липиды", None, ["Удерживать жиры < 28 г", "Принимать псиллиум"], [], [])
    assert out["verdict"].startswith("Жиры менее 28 г")
    assert out["short_actions"] == ["Жиры < 28 г/день", "Псиллиум 1 ч. л. в 12:00"]


def test_family_verdict_misaligned_short_actions_fall_back_to_clipped_originals(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {"verdict": "Итог", "short_actions": ["только одно"]})
    long_a = "Удерживать насыщенные жиры ≤28 г/день с растворимой клетчаткой и пересдать липидограмму через 90 дней"
    out = cs.family_verdict("липиды", None, [long_a, "Принимать псиллиум"], [], [])
    assert out["verdict"] == "Итог" and len(out["short_actions"]) == 2
    assert all(len(x) <= cs.FAMILY_SHORT_MAX + 1 for x in out["short_actions"])


def test_family_verdict_llm_failure_gives_no_verdict_but_short_actions(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: {})
    out = cs.family_verdict("липиды", None, ["Принимать псиллиум"], [], [])
    assert out["verdict"] is None and out["short_actions"] == ["Принимать псиллиум"]


def test_family_verdict_no_actions_is_legitimate_no_llm(monkeypatch):
    monkeypatch.setattr(cs, "_call_llm", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM не нужен")))
    assert cs.family_verdict("тема", None, [], [], []) == {"verdict": cs.NO_CHANGES_VERDICT, "short_actions": []}


def test_verdict_message_is_short_and_has_no_debate():
    msg = cs.build_verdict_message("Жиры менее 28 г в день.")
    assert msg.startswith("🩺 Врачи посовещались и решили:") and "Жиры менее 28 г в день." in msg
    assert len(msg) < 300


def test_ensure_verdict_fills_once_and_is_idempotent(monkeypatch):
    from psycopg import sql
    rid = "cs_TEST_verdict"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("INSERT INTO {t} (id, topic, actions, status) VALUES (%s, %s, %s, 'completed')")
                    .format(t=sql.Identifier(schema(), "consilium_report")),
                    (rid, "тест", json.dumps([{"imperative": "Принимать псиллиум", "accepted": True}])))
        conn.commit()
    calls = []
    monkeypatch.setattr(cs, "family_verdict", lambda *a, **kw: calls.append(1) or
                        {"verdict": "Псиллиум 1 ч. л. в 12:00.", "short_actions": ["Псиллиум 1 ч. л."]})
    assert cs.ensure_verdict(rid) == "Псиллиум 1 ч. л. в 12:00."
    assert cs.ensure_verdict(rid) == "Псиллиум 1 ч. л. в 12:00." and len(calls) == 1  # второй раз LLM не зовём
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT actions FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), "consilium_report")), (rid,))
        assert cur.fetchone()[0][0]["short"] == "Псиллиум 1 ч. л."
