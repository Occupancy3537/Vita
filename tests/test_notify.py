"""app/notify.py — единственная точка инициативной отправки (ROADMAP 5.1).
Юниты на бюджет/приоритеты против реального card.notify_log (изолировано
через _isolate_real_schema_writes — ROADMAP 0.7, см. tests/conftest.py)."""
import pytest

from app import notify
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _sent_count(cur):
    cur.execute(f"SELECT count(*) FROM {schema()}.notify_log")
    return cur.fetchone()[0]


def test_red_flag_always_sends_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: calls.append(text) or True)
    res = notify.notify("test_source", "red_flag", "тревога")
    assert res == {"sent_immediately": True, "ok": True}
    assert calls == ["тревога"]


def test_red_flag_ignores_exhausted_critical_budget(monkeypatch):
    """red_flag вне бюджета в принципе — не смотрит на critical-счётчик."""
    calls = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: calls.append(text) or True)
    for _ in range(5):  # заведомо больше CRITICAL_DAILY_BUDGET
        notify.notify("noise", "critical", "шум")
    res = notify.notify("doctor", "red_flag", "срочно")
    assert res["sent_immediately"] is True
    assert calls[-1] == "срочно"


def test_critical_sends_immediately_within_budget(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    for i in range(notify.CRITICAL_DAILY_BUDGET):
        res = notify.notify("gate_watch", "critical", f"событие {i}")
        assert res == {"sent_immediately": True, "ok": True}
    assert len(sent) == notify.CRITICAL_DAILY_BUDGET


def test_critical_over_budget_goes_to_digest_not_lost(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    for i in range(notify.CRITICAL_DAILY_BUDGET):
        notify.notify("gate_watch", "critical", f"в бюджете {i}")
    res = notify.notify("gate_watch", "critical", "сверх бюджета")
    assert res == {"sent_immediately": False, "ok": None}
    assert "сверх бюджета" not in sent  # не ушло немедленно...
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT priority, immediate FROM {schema()}.notify_log WHERE text = %s", ("сверх бюджета",))
        row = cur.fetchone()
    assert row == ("critical", False)  # ...но и не потеряно — залогировано для дайджеста


def test_normal_and_digest_never_send_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: calls.append(text) or True)
    r1 = notify.notify("nutrition_reports", "normal", "сводка питания")
    r2 = notify.notify("weekly_advisor", "digest", "недельный отчёт")
    assert r1 == {"sent_immediately": False, "ok": None}
    assert r2 == {"sent_immediately": False, "ok": None}
    assert calls == []


def test_unknown_priority_raises():
    with pytest.raises(ValueError):
        notify.notify("x", "urgent", "текст")


def test_every_call_logs_source_priority_immediate_ts():
    """Приёмка #7: журнал — источник/приоритет/немедленно-или-нет/время."""
    with get_conn() as conn, conn.cursor() as cur:
        before = _sent_count(cur)
    notify.notify("health_watchdog", "normal", "нудж")
    with get_conn() as conn, conn.cursor() as cur:
        after = _sent_count(cur)
        cur.execute(
            f"SELECT source, priority, immediate, ts IS NOT NULL FROM {schema()}.notify_log "
            "WHERE text = %s", ("нудж",),
        )
        row = cur.fetchone()
    assert after == before + 1
    assert row == ("health_watchdog", "normal", False, True)


def test_log_external_send_records_metadata_without_text():
    """doctor/intake.py::_deliver_emergency — сам шлёт (3 попытки + фолбэк),
    сюда только журналируется факт, без клинического текста (уже есть в
    card.rf_event/journal — не дублируем)."""
    notify.log_external_send("doctor_emergency", "red_flag")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT priority, immediate, text FROM {schema()}.notify_log "
            "WHERE source = 'doctor_emergency' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row == ("red_flag", True, None)


def test_log_external_send_does_not_count_against_critical_budget(monkeypatch):
    """Эмердженси не должен незаметно съедать бюджет critical у других
    отправителей — red_flag вне бюджета целиком, включая внешние отправки."""
    sent = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    for _ in range(10):
        notify.log_external_send("doctor_emergency", "red_flag")
    for i in range(notify.CRITICAL_DAILY_BUDGET):
        res = notify.notify("gate_watch", "critical", f"событие {i}")
        assert res["sent_immediately"] is True
