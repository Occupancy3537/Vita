"""Follow-up по открытым сессиям красных флагов: чистые тесты run_once.
Фиктивная отправка (monkeypatch _deliver_emergency); синтетические id rfs_TEST…;
боевые таблицы не пишутся по конструкции _isolate_real_schema_writes."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import redflag_followup as rf


@pytest.fixture()
def _fake_send(monkeypatch):
    """Заменяет _deliver_emergency на счётчик."""
    calls = []

    def fake(chat_id, message_id, text):
        calls.append({"chat_id": chat_id, "text": text})
        return True

    monkeypatch.setattr(rf, "_get_deliver", lambda: fake)
    return calls


@pytest.fixture()
def _failing_send(monkeypatch):
    """Заменяет _deliver_emergency на всегда падающую."""
    monkeypatch.setattr(rf, "_get_deliver", lambda: lambda *a, **kw: False)
    return None


@pytest.fixture()
def _since_set(monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", "2026-09-01T00:00:00+00:00")


@pytest.fixture()
def _since_empty(monkeypatch):
    monkeypatch.delenv("RF_FOLLOWUP_SINCE", raising=False)


NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
BASE = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)  # 50 часов назад от NOW


def _insert_session(cur, sid, category="severe_pain", level="L2",
                    opened=None, last_activity=None, status="open"):
    cur.execute(
        "INSERT INTO card_test.rf_session (id, category, worst_level, status, opened_ts, last_activity) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (sid, category, level, status, opened or BASE, last_activity or BASE),
    )


def _insert_followup(cur, sid, attempts=1, sent_ts=None, last_error=None):
    cur.execute(
        "INSERT INTO card_test.rf_followup (session_id, sent_ts, attempts, last_error) "
        "VALUES (%s, %s, %s, %s)",
        (sid, sent_ts, attempts, last_error),
    )


def _session(cur, sid):
    cur.execute("SELECT 1 FROM card_test.rf_session WHERE id = %s", (sid,))
    return cur.fetchone() is not None


def _followup(cur, sid):
    cur.execute("SELECT sent_ts, attempts, last_error FROM card_test.rf_followup WHERE session_id = %s", (sid,))
    return cur.fetchone()


# ── RF_FOLLOWUP_SINCE ────────────────────────────────────────────────────────

@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_empty")
def test_since_empty_does_nothing():
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_stale")
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["skipped_no_since"] is True
    assert result["sent"] == 0


# ── фильтры: возраст, уровень, статус ────────────────────────────────────────

@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_fake_send")
def test_session_younger_than_48h_not_sent():
    from app.db import get_conn
    young = NOW - timedelta(hours=20)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_young", last_activity=young)
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 0


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_fake_send")
def test_session_older_than_since_not_sent():
    from app.db import get_conn
    # opened_ts ДОЖЕ since → не трогаем (старая сессия от 15.09)
    old = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)
    stale_la = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_old_15sep", opened=old, last_activity=stale_la)
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 0


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_fake_send")
def test_l1_session_not_sent():
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_l1", level="L1", last_activity=stale)
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 0


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_fake_send")
def test_closed_session_not_sent():
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_closed", status="closed", last_activity=stale)
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 0


# ── базовый поток ────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set")
def test_eligible_session_sent_once(_fake_send):
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_good", last_activity=stale)
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 1
    assert len(_fake_send) == 1
    assert "сильная боль" in _fake_send[0]["text"]
    # второй run_once → не шлёт (идемпотентность)
    result2 = rf.run_once(now=NOW)
    assert result2["sent"] == 0
    assert len(_fake_send) == 1  # всё ещё одна отправка


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set")
def test_text_contains_human_topic_not_raw_key(_fake_send):
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_topic", category="cardiac_acute", last_activity=stale)
        conn.commit()
    rf.run_once(now=NOW)
    assert len(_fake_send) == 1
    assert "cardiac_acute" not in _fake_send[0]["text"]
    assert "сердце" in _fake_send[0]["text"]


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set")
def test_unknown_category_fallback_text(_fake_send):
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_unknown", category="weird_new_cat", last_activity=stale)
        conn.commit()
    rf.run_once(now=NOW)
    assert len(_fake_send) == 1
    assert "weird_new_cat" not in _fake_send[0]["text"]


# ── попытки отправки ─────────────────────────────────────────────────────────

@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_failing_send")
def test_failed_send_increments_attempts_no_sent_ts():
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_fail")
        conn.commit()
    rf.run_once(now=NOW)
    fu = _followup(cur := get_conn().cursor() if False else get_conn().cursor(), "rfs_TEST_fail")
    # Используем отдельное соединение для проверки
    with get_conn() as conn, conn.cursor() as cur:
        fu = _followup(cur, "rfs_TEST_fail")
    assert fu is not None
    assert fu[1] == 1  # attempts = 1
    assert fu[0] is None  # sent_ts = None (не отправлено)
    assert fu[2] is not None  # last_error не пустой


@pytest.mark.usefixtures("_isolate_real_schema_writes", "_since_set", "_failing_send")
def test_after_3_failed_attempts_no_more_tries():
    from app.db import get_conn
    stale = NOW - timedelta(hours=72)
    with get_conn() as conn, conn.cursor() as cur:
        _insert_session(cur, "rfs_TEST_maxed")
        _insert_followup(cur, "rfs_TEST_maxed", attempts=3, last_error="prev")
        conn.commit()
    result = rf.run_once(now=NOW)
    assert result["sent"] == 0
    with get_conn() as conn, conn.cursor() as cur:
        fu = _followup(cur, "rfs_TEST_maxed")
    assert fu[1] == 3  # attempts не изменился (сессия отфильтрована запросом)
