"""«Детектив» (2026-09-26, часть 1-2) — app/problem.py: create/close жизненного
цикла card.problem (до этого тикета путей не существовало вовсе) + автопривязка
symptom_key -> активная problem для новых эпизодов + presumed_resolved по тишине."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from ulid import ULID

from app import problem as pm
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _insert_episode(symptom_key: str, onset_days_ago: int = 0, status: str = "open",
                    problem_id=None, context=None) -> str:
    ep_id = f"ep_{ULID()}"
    onset = datetime.now(timezone.utc) - timedelta(days=onset_days_ago)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.episode (id, ts_event, provenance, symptom_key, onset_ts, status, problem_id, context) "
            "VALUES (%s, now(), '{}', %s, %s, %s, %s, %s)",
            (ep_id, symptom_key, onset, status, problem_id, context),
        )
        conn.commit()
    return ep_id


def _insert_fact_for_episode(episode_id: str, ts=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, episode_id) "
            "VALUES (%s, %s, '{}', 'auto', 'symptom:test', %s)",
            (f"f_{ULID()}", ts or datetime.now(timezone.utc), episode_id),
        )
        conn.commit()


def _fetch_episode(ep_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT status, problem_id, end_ts, closure_source FROM {schema()}.episode WHERE id = %s", (ep_id,)
        )
        return cur.fetchone()


def _fetch_problem(problem_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT title, status, case_summary, closed_ts FROM {schema()}.problem WHERE id = %s", (problem_id,)
        )
        return cur.fetchone()


# ─────── create_problem ───────

def test_create_problem_inserts_active_row():
    with get_conn() as conn, conn.cursor() as cur:
        result = pm.create_problem(cur, "Тестовая проблема")
        conn.commit()
    title, status, case_summary, closed_ts = _fetch_problem(result["id"])
    assert title == "Тестовая проблема"
    assert status == "active"
    assert closed_ts is None
    assert result["linked_episodes"] == 0


def test_create_problem_links_existing_unclaimed_episodes_by_symptom_key():
    ep1 = _insert_episode("test-key-link-1")
    ep2 = _insert_episode("test-key-link-1", onset_days_ago=1)
    with get_conn() as conn, conn.cursor() as cur:
        result = pm.create_problem(cur, "Проблема со связкой", symptom_keys=["test-key-link-1"])
        conn.commit()
    assert result["linked_episodes"] == 2
    assert result["skipped_already_linked"] == 0
    assert _fetch_episode(ep1)[1] == result["id"]
    assert _fetch_episode(ep2)[1] == result["id"]


def test_create_problem_does_not_steal_episode_already_linked_to_another_problem():
    with get_conn() as conn, conn.cursor() as cur:
        other = pm.create_problem(cur, "Другая проблема")
        conn.commit()
    _insert_episode("test-key-taken", problem_id=other["id"])

    with get_conn() as conn, conn.cursor() as cur:
        result = pm.create_problem(cur, "Новая проблема", symptom_keys=["test-key-taken"])
        conn.commit()
    assert result["linked_episodes"] == 0
    assert result["skipped_already_linked"] == 1


# ─────── close_problem ───────

def test_close_problem_sets_status_and_case_summary():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Закрываемая проблема")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        result = pm.close_problem(cur, created["id"], "resolved", "Прошло само за неделю", what_helped="покой")
        conn.commit()
    assert result is not None
    title, status, case_summary, closed_ts = _fetch_problem(created["id"])
    assert status == "resolved"
    assert closed_ts is not None
    assert case_summary["summary"] == "Прошло само за неделю"
    assert case_summary["what_helped"] == "покой"


def test_close_problem_closes_open_episodes_with_final_status():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Проблема с эпизодами")
        conn.commit()
    ep_open = _insert_episode("test-key-closeep", problem_id=created["id"], status="open")
    ep_resolved = _insert_episode("test-key-closeep", problem_id=created["id"], status="resolved")

    with get_conn() as conn, conn.cursor() as cur:
        pm.close_problem(cur, created["id"], "resolved", "разобрались")
        conn.commit()

    status, _, end_ts, closure_source = _fetch_episode(ep_open)
    assert status == "resolved" and end_ts is not None and closure_source == "problem_closed"
    # уже закрытый эпизод не трогаем повторно (closure_source не перезаписан на problem_closed)
    status2, _, _, closure_source2 = _fetch_episode(ep_resolved)
    assert status2 == "resolved" and closure_source2 != "problem_closed"


def test_close_problem_returns_none_for_unknown_id():
    with get_conn() as conn, conn.cursor() as cur:
        result = pm.close_problem(cur, "pb_does_not_exist", "resolved", "текст")
        conn.commit()
    assert result is None


def test_close_problem_returns_none_if_already_closed():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Дважды закрываемая")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        pm.close_problem(cur, created["id"], "resolved", "первый раз")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        second = pm.close_problem(cur, created["id"], "chronic", "второй раз")
        conn.commit()
    assert second is None


def test_close_problem_writes_case_summary_memory_note():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Проблема для памяти")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        pm.close_problem(cur, created["id"], "resolved", "итоговый текст для памяти")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT type, content FROM {schema()}.memory_note WHERE type = 'case_summary' "
            f"AND content->>'problem_id' = %s",
            (created["id"],),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[1]["summary"] == "итоговый текст для памяти"


# ─────── link_new_episode (Часть 2.1) ───────

def test_link_new_episode_links_when_exactly_one_active_problem():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Единственная активная")
        conn.commit()
    _insert_episode("test-key-single", problem_id=created["id"])  # прежний эпизод той же темы
    new_ep = _insert_episode("test-key-single")  # новый, ещё без problem_id

    with get_conn() as conn, conn.cursor() as cur:
        linked = pm.link_new_episode(cur, "test-key-single", new_ep)
        conn.commit()
    assert linked == created["id"]
    assert _fetch_episode(new_ep)[1] == created["id"]


def test_link_new_episode_no_op_when_no_active_problem_uses_this_key():
    new_ep = _insert_episode("test-key-orphan")
    with get_conn() as conn, conn.cursor() as cur:
        linked = pm.link_new_episode(cur, "test-key-orphan", new_ep)
        conn.commit()
    assert linked is None
    assert _fetch_episode(new_ep)[1] is None


def test_link_new_episode_does_not_guess_when_ambiguous_and_logs_issue():
    with get_conn() as conn, conn.cursor() as cur:
        p1 = pm.create_problem(cur, "Проблема А")
        p2 = pm.create_problem(cur, "Проблема Б")
        conn.commit()
    _insert_episode("test-key-ambig", problem_id=p1["id"])
    _insert_episode("test-key-ambig", problem_id=p2["id"])
    new_ep = _insert_episode("test-key-ambig")

    with get_conn() as conn, conn.cursor() as cur:
        linked = pm.link_new_episode(cur, "test-key-ambig", new_ep)
        conn.commit()
    assert linked is None
    assert _fetch_episode(new_ep)[1] is None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT summary FROM {schema()}.issue_log WHERE natural_key = %s",
            ("episode_problem_ambiguous:test-key-ambig",),
        )
        row = cur.fetchone()
    assert row is not None
    assert "test-key-ambig" in row[0]


def test_link_new_episode_ignores_closed_problems():
    with get_conn() as conn, conn.cursor() as cur:
        created = pm.create_problem(cur, "Уже закрытая")
        conn.commit()
    _insert_episode("test-key-closedproblem", problem_id=created["id"])
    with get_conn() as conn, conn.cursor() as cur:
        pm.close_problem(cur, created["id"], "resolved", "закрыто")
        conn.commit()
    new_ep = _insert_episode("test-key-closedproblem")

    with get_conn() as conn, conn.cursor() as cur:
        linked = pm.link_new_episode(cur, "test-key-closedproblem", new_ep)
        conn.commit()
    assert linked is None


# ─────── run_daily_maintenance (Часть 2.2) ───────

def test_run_daily_maintenance_marks_silent_open_episode_presumed_resolved():
    old_ts = datetime.now(timezone.utc) - timedelta(days=pm.PRESUMED_RESOLVED_SILENCE_DAYS + 5)
    ep_id = _insert_episode("test-key-silent", onset_days_ago=pm.PRESUMED_RESOLVED_SILENCE_DAYS + 5)
    _insert_fact_for_episode(ep_id, ts=old_ts)

    updated = pm.run_daily_maintenance()

    status, _, end_ts, closure_source = _fetch_episode(ep_id)
    assert status == "presumed_resolved"
    assert closure_source == "presumed_timeout"
    assert end_ts is not None
    assert updated >= 1


def test_run_daily_maintenance_leaves_recent_open_episode_alone():
    ep_id = _insert_episode("test-key-recent", onset_days_ago=2)
    pm.run_daily_maintenance()
    status, _, end_ts, _ = _fetch_episode(ep_id)
    assert status == "open"
    assert end_ts is None


def test_run_daily_maintenance_leaves_already_resolved_episode_alone():
    ep_id = _insert_episode("test-key-alreadyres", onset_days_ago=60, status="resolved")
    pm.run_daily_maintenance()
    status, _, _, closure_source = _fetch_episode(ep_id)
    assert status == "resolved"
    assert closure_source != "presumed_timeout"


# ─────── run_scheduler ───────

def test_run_scheduler_calls_maintenance_and_marks_run(monkeypatch):
    calls = []
    monkeypatch.setattr(pm, "run_daily_maintenance", lambda: calls.append("ran") or 0)
    monkeypatch.setattr(pm.run_log, "mark_run", lambda name: calls.append("marked"))

    # Первый sleep — обычное пробуждение (нет догоняющего тика у этого
    # планировщика, он просыпается и работает раз в сутки); второй —
    # прерывание, чтобы не крутить бесконечный while в тесте.
    sleep_calls = {"n": 0}

    def sleep_once_then_stop(*a, **kw):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:
            raise KeyboardInterrupt()
    monkeypatch.setattr(pm.timeutil, "sleep_until_local", sleep_once_then_stop)

    with pytest.raises(KeyboardInterrupt):
        pm.run_scheduler()
    assert calls == ["ran", "marked"]
