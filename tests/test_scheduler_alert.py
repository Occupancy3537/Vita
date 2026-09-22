"""app/scheduler_alert.py — общий алерт на падение фонового цикла (см.
докстринг модуля, AGENT_SYNC #38/#40). run_notify реально пишет в
card.err_dedup_state — cleanup тестового ключа обязателен. 2026-09-23:
run_notify() дополнительно пишет в card.issue_log (Шаг 1 «петли
самоулучшения», см. app/issue_log.py) — тот же cleanup нужен и там,
иначе тестовая находка навсегда остаётся «открытой» в бэклоге."""
import pytest

from app import scheduler_alert as sa
from app.db import get_conn, schema

TEST_KEY = "test-scheduler-alert-source|scheduler"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM card.err_dedup_state WHERE key = %s", (TEST_KEY,))
        cur.execute(f"DELETE FROM {schema()}.issue_log WHERE natural_key = %s",
                    ("errdedup:test-scheduler-alert-source:scheduler",))
        conn.commit()


def test_alert_on_failure_sends_and_records(monkeypatch):
    sent = []
    # 2026-09-21: алерты -> Hermes, не бот доктора (err_dedup.run_notify зовёт app.hermes_telegram)
    monkeypatch.setattr("app.hermes_telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("boom"))
    assert len(sent) == 1
    assert "test-scheduler-alert-source" in sent[0]
    assert "boom" in sent[0]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_notified_at FROM card.err_dedup_state WHERE key = %s", (TEST_KEY,))
        assert cur.fetchone() is not None


def test_alert_on_failure_deduped_within_window(monkeypatch):
    sent = []
    monkeypatch.setattr("app.hermes_telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("first"))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("second"))
    assert len(sent) == 1  # второй в пределах 60 мин подавлен


def test_alert_on_failure_never_raises_when_db_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no db")
    monkeypatch.setattr(sa, "get_conn", _boom)
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("x"))  # не должно бросить


# --- alert_on_sustained_failure (2026-09-23, по запросу Влада: не шуметь на --
# единичный сетевой обрыв long-polling, который retry-цикл сам переживает) --

def test_alert_on_sustained_failure_below_threshold_is_silent(monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    sa.alert_on_sustained_failure("test-scheduler-alert-source", ValueError("blip"), consecutive_failures=1)
    sa.alert_on_sustained_failure("test-scheduler-alert-source", ValueError("blip"), consecutive_failures=2)
    assert calls == []  # ниже порога (по умолчанию 3) — ни разу не позвал alert_on_failure


def test_alert_on_sustained_failure_at_threshold_fires(monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    sa.alert_on_sustained_failure("test-scheduler-alert-source", ValueError("сеть совсем легла"), consecutive_failures=3)
    assert len(calls) == 1
    assert calls[0][0] == "test-scheduler-alert-source"


def test_alert_on_sustained_failure_custom_threshold(monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    sa.alert_on_sustained_failure("x", ValueError("y"), consecutive_failures=1, threshold=1)
    assert len(calls) == 1  # порог=1 — алертит с первого раза, как раньше alert_on_failure
