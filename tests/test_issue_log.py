"""app/issue_log.py — durable-бэклог продуктовых находок (2026-09-23, Шаг 1
«петли самоулучшения»). Реальная таблица card_test.issue_log (см.
tests/conftest.py — CARD_PG_SCHEMA=card_test под pytest), тестовые
natural_key с префиксом, cleanup."""
import pytest

from app import issue_log as il
from app.db import get_conn, schema

TEST_KEY = "test:issue_log:demo"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema()}.issue_log WHERE natural_key LIKE 'test:issue_log:%'")
        conn.commit()


def _read(key):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT source, severity, summary, status, occurrences, resolved_at, resolution_ref "
            f"FROM {schema()}.issue_log WHERE natural_key = %s",
            (key,),
        )
        return cur.fetchone()


def test_record_issue_creates_open_row():
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="weekly_advisor", summary="упал: boom")
        conn.commit()
    row = _read(TEST_KEY)
    assert row == ("weekly_advisor", "important", "упал: boom", "open", 1, None, None)


def test_record_issue_repeat_bumps_occurrences_and_last_seen():
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="первый раз")
        il.record_issue(cur, TEST_KEY, source="x", summary="второй раз, текст свежее")
        conn.commit()
    row = _read(TEST_KEY)
    assert row[2] == "второй раз, текст свежее"  # summary — самый свежий
    assert row[4] == 2                            # occurrences выросли


def test_record_issue_invalid_severity_falls_back_to_important():
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="s", severity="что-то не то")
        conn.commit()
    assert _read(TEST_KEY)[1] == "important"


def test_record_issue_never_raises_on_db_error(monkeypatch):
    """Fail-safe: вызывается из except-блоков, вторичный сбой не должен
    маскировать исходную ошибку (тот же принцип, что у app/run_log.py)."""
    class BoomCursor:
        def execute(self, *a, **k):
            raise RuntimeError("db упал")
    il.record_issue(BoomCursor(), TEST_KEY, source="x", summary="s")  # не бросает


def test_resolve_issue_marks_fixed_with_reference():
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="s")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        ok = il.resolve_issue(cur, TEST_KEY, resolution_ref="commit abc123")
        conn.commit()
    assert ok is True
    row = _read(TEST_KEY)
    assert row[3] == "fixed" and row[5] is not None and row[6] == "commit abc123"


def test_resolve_issue_nonexistent_key_returns_false_no_crash():
    with get_conn() as conn, conn.cursor() as cur:
        ok = il.resolve_issue(cur, "test:issue_log:никогда-не-было", resolution_ref="x")
    assert ok is False


def test_record_issue_reopens_a_fixed_issue_on_recurrence():
    """Повтор чинённого — сигнал сам по себе, не тихое молчание."""
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="было")
        il.resolve_issue(cur, TEST_KEY, resolution_ref="commit abc")
        conn.commit()
    assert _read(TEST_KEY)[3] == "fixed"
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="снова случилось")
        conn.commit()
    row = _read(TEST_KEY)
    assert row[3] == "open"       # переоткрыт
    assert row[4] == 2            # occurrences продолжают расти, не сбрасываются


def test_record_issue_does_not_reopen_wontfix():
    """'wontfix' — уже принятое решение, повтор не должен его тихо отменять."""
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="было")
        il.resolve_issue(cur, TEST_KEY, resolution_ref="осознанно не чиним", status="wontfix")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        il.record_issue(cur, TEST_KEY, source="x", summary="опять")
        conn.commit()
    row = _read(TEST_KEY)
    assert row[3] == "wontfix"    # статус не тронут
    assert row[4] == 2            # но что оно повторяется — видно
