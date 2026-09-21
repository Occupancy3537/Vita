"""app/scheduler_alert.py — общий алерт на падение фонового цикла (см.
докстринг модуля, AGENT_SYNC #38/#40). run_notify реально пишет в
card.err_dedup_state — cleanup тестового ключа обязателен."""
import pytest

from app import scheduler_alert as sa
from app.db import get_conn

TEST_KEY = "test-scheduler-alert-source|scheduler"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM card.err_dedup_state WHERE key = %s", (TEST_KEY,))
        conn.commit()


def test_alert_on_failure_sends_and_records(monkeypatch):
    sent = []
    monkeypatch.setattr("app.doctor.telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("boom"))
    assert len(sent) == 1
    assert "test-scheduler-alert-source" in sent[0]
    assert "boom" in sent[0]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_notified_at FROM card.err_dedup_state WHERE key = %s", (TEST_KEY,))
        assert cur.fetchone() is not None


def test_alert_on_failure_deduped_within_window(monkeypatch):
    sent = []
    monkeypatch.setattr("app.doctor.telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("first"))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("second"))
    assert len(sent) == 1  # второй в пределах 60 мин подавлен


def test_alert_on_failure_never_raises_when_db_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no db")
    monkeypatch.setattr(sa, "get_conn", _boom)
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("x"))  # не должно бросить
