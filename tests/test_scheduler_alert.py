"""app/scheduler_alert.py — общий алерт на падение фонового цикла (см.
докстринг модуля, AGENT_SYNC #38/#40). run_notify реально пишет в
card.err_dedup_state — хардкожена буквально (не через schema()), писала
в БОЕВОЙ card даже под CARD_PG_SCHEMA=card_test. Изолировано через
_isolate_real_schema_writes (ROADMAP 0.7, 2026-09-24)."""
import pytest

from app import scheduler_alert as sa
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")

TEST_KEY = "test-scheduler-alert-source|scheduler"


def test_alert_on_failure_sends_and_records(monkeypatch):
    sent = []
    # 2026-09-21: алерты -> сервисный бот, не бот доктора (err_dedup.run_notify зовёт app.service_telegram)
    monkeypatch.setattr("app.service_telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("boom"))
    assert len(sent) == 1
    assert "test-scheduler-alert-source" in sent[0]
    assert "boom" in sent[0]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_notified_at FROM card.err_dedup_state WHERE key = %s", (TEST_KEY,))
        assert cur.fetchone() is not None


def test_alert_on_failure_deduped_within_window(monkeypatch):
    sent = []
    monkeypatch.setattr("app.service_telegram.send_message", lambda chat_id, text, **kw: sent.append(text))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("first"))
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("second"))
    assert len(sent) == 1  # второй в пределах 60 мин подавлен


def test_alert_on_failure_never_raises_when_db_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no db")
    monkeypatch.setattr(sa, "get_conn", _boom)
    sa.alert_on_failure("test-scheduler-alert-source", ValueError("x"))  # не должно бросить


# --- alert_on_sustained_failure (2026-09-23, по запросу Влада: не шуметь на --
# единичный/непродолжительный сетевой обрыв long-polling, который retry-цикл
# сам переживает; версия 2 того же дня — считает РЕАЛЬНОЕ непрерывное время,
# не число попыток подряд, см. её докстринг про живой 4-минутный инцидент) --

def test_alert_on_sustained_failure_recent_start_is_silent(monkeypatch):
    from datetime import datetime, timedelta, timezone
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    just_now = datetime.now(timezone.utc) - timedelta(seconds=30)
    sa.alert_on_sustained_failure("test-scheduler-alert-source", ValueError("blip"), just_now)
    assert calls == []  # сбой идёт всего 30с (< 5 мин по умолчанию) — не алерчу


def test_alert_on_sustained_failure_past_min_duration_fires(monkeypatch):
    from datetime import datetime, timedelta, timezone
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=6)
    sa.alert_on_sustained_failure("test-scheduler-alert-source", ValueError("сеть совсем легла"), long_ago)
    assert len(calls) == 1
    assert calls[0][0] == "test-scheduler-alert-source"


def test_alert_on_sustained_failure_custom_min_duration(monkeypatch):
    from datetime import datetime, timedelta, timezone
    calls = []
    monkeypatch.setattr(sa, "alert_on_failure", lambda src, exc: calls.append((src, exc)))
    ten_seconds_ago = datetime.now(timezone.utc) - timedelta(seconds=10)
    sa.alert_on_sustained_failure("x", ValueError("y"), ten_seconds_ago, min_duration_seconds=5)
    assert len(calls) == 1  # порог=5с — 10с сбоя уже достаточно
