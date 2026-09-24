"""app/issue_review.py — еженедельный разбор нерешённых находок (2026-09-23,
по прямому запросу Влада после инцидента с ложными предупреждениями на
«Настройках»: критичное — сразу в вердикт, остальное — раз в неделю, тишина
в остальное время)."""
import pytest

from app import issue_log, issue_review as ir
from app.db import get_conn, schema

TEST_PREFIX = "test:issue_review:"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema()}.issue_log WHERE natural_key LIKE %s", (f"{TEST_PREFIX}%",))
        conn.commit()


def test_build_digest_text_empty_backlog_is_none():
    assert ir.build_digest_text([]) is None


def test_build_digest_text_lists_source_and_summary():
    from datetime import datetime, timezone
    rows = [{"natural_key": "x", "source": "weekly_advisor", "severity": "important",
             "summary": "модель не ответила", "occurrences": 3, "first_seen": datetime.now(timezone.utc)}]
    text = ir.build_digest_text(rows)
    assert "weekly_advisor" in text
    assert "модель не ответила" in text
    assert "3×" in text


def test_pick_undecided_excludes_critical_and_decided(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        issue_log.record_issue(cur, f"{TEST_PREFIX}important", source="a", summary="важное", severity="important")
        issue_log.record_issue(cur, f"{TEST_PREFIX}critical", source="b", summary="критичное", severity="critical")
        issue_log.record_issue(cur, f"{TEST_PREFIX}fixed", source="c", summary="починено", severity="minor")
        issue_log.resolve_issue(cur, f"{TEST_PREFIX}fixed", resolution_ref="commit x")
        issue_log.record_issue(cur, f"{TEST_PREFIX}snoozed", source="d", summary="отложено", severity="minor")
        issue_log.resolve_issue(cur, f"{TEST_PREFIX}snoozed", resolution_ref="не сейчас", status="snoozed")
        conn.commit()
        rows = ir._pick_undecided(cur)
    sources = {r["source"] for r in rows}
    assert sources == {"a"}  # только открытая НЕ-critical находка


def test_run_once_sends_when_backlog_nonempty(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        issue_log.record_issue(cur, f"{TEST_PREFIX}pending", source="x", summary="ждёт решения", severity="important")
        conn.commit()
    sent = []
    monkeypatch.setattr(ir.notify, "notify", lambda source, priority, text: sent.append((source, priority, text)))
    n = ir.run_once()
    assert n == 1
    assert len(sent) == 1
    assert sent[0][0] == "issue_review" and sent[0][1] == "normal"
    assert "ждёт решения" in sent[0][2]


def test_run_once_silent_when_backlog_empty(monkeypatch):
    sent = []
    monkeypatch.setattr(ir.notify, "notify", lambda source, priority, text: sent.append(text))
    n = ir.run_once()
    assert n == 0
    assert sent == []  # тишина — не новый источник шума
