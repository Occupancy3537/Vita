"""app/backup_alert.py — порт n8n `_Backup Alert` (2026-09-20). health.
backup_alert_state — реальная прод-таблица (1 строка-синглтон), сбрасываем
до/после каждого теста, тот же принцип, что test_gate_watch.py."""
import pytest
from datetime import datetime, timedelta, timezone

from app import backup_alert as ba
from app.db import get_conn


@pytest.fixture(autouse=True)
def reset_state():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.backup_alert_state WHERE id = 1")
        conn.commit()
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.backup_alert_state WHERE id = 1")
        conn.commit()


def test_handle_ping_wrong_token_is_silent_and_does_not_store():
    assert ba.handle_ping("wrong", "ok", "", "2026-09-20 04:00") == ""
    with get_conn() as conn, conn.cursor() as cur:
        assert ba._load_state(cur) is None


def test_handle_ping_ok_result_no_alert_but_stores():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "ok", "", "2026-09-20 04:00")
    assert text == ""
    with get_conn() as conn, conn.cursor() as cur:
        state = ba._load_state(cur)
    assert state["result"] == "ok"


def test_handle_ping_failed_result_alerts():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "failed", "rclone timeout", "2026-09-20 04:00")
    assert "СБОЙ" in text
    assert "rclone timeout" in text


def test_handle_ping_partial_result_alerts_softly():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "partial", "gdrive auth expired", "2026-09-20 04:00")
    assert "один провайдер не сработал" in text
    assert "gdrive auth expired" in text


def test_handle_ping_unknown_result_alerts():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "weird", "?", "2026-09-20 04:00")
    assert "weird" in text


def test_check_stale_never_pinged():
    assert "никогда" in ba.check_stale()


def test_check_stale_recent_ok_ping_is_silent():
    ba.handle_ping(ba.WEBHOOK_TOKEN, "ok", "", "now")
    assert ba.check_stale() == ""


def test_check_stale_old_ping_alerts(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO health.backup_alert_state (id, ts, result, detail, stamp) "
            "VALUES (1, now() - interval '30 hours', 'ok', '', 'вчера')"
        )
        conn.commit()
    assert "не отработал" in ba.check_stale()


def test_check_stale_recent_but_failed_result_alerts():
    ba.handle_ping(ba.WEBHOOK_TOKEN, "failed", "diskfull", "2026-09-20 04:00")
    assert "последний прогон со сбоем" in ba.check_stale()


def test_run_once_sends_only_on_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(ba, "check_stale", lambda: "⚠️ тест")
    monkeypatch.setattr(ba.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    ba.run_once()
    assert calls == [((ba.CHAT_ID, "⚠️ тест"), {"parse_mode": "HTML"})]


def test_run_once_no_send_when_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(ba, "check_stale", lambda: "")
    monkeypatch.setattr(ba.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    ba.run_once()
    assert calls == []
