"""app/gate_watch.py — алерт на снятие/возврат гейта нагрузки (порт из n8n
today-dashboard Build Today JSON, 2026-09-20). health.gate_state — реальная
прод-таблица (1 строка-синглтон, health.* тестовой копии нет, тот же принцип,
что test_dashboard_bioage.py/test_dashboard_today.py) — сбрасываем строку
до/после каждого теста, чтобы тесты не зависели от порядка запуска и не
трогали реальное текущее состояние дольше теста."""
import pytest

from app import gate_watch
from app.db import get_conn


@pytest.fixture(autouse=True)
def reset_gate_state():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.gate_state WHERE id = 1")
        conn.commit()
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.gate_state WHERE id = 1")
        conn.commit()


def _fake_today(blocked, source="тест"):
    return {"decision": {"gate": {"blocked": blocked, "source": source}}}


def test_store_and_read_blocked_roundtrip():
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is None
        gate_watch._store_blocked(cur, True)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is True
        gate_watch._store_blocked(cur, False)  # ON CONFLICT ветка, не INSERT
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is False


def test_check_once_no_alert_on_first_ever_run(monkeypatch):
    """Нет строки в gate_state = не с чем сравнивать — не алертим на переход,
    которого не видели, просто запоминаем текущее состояние."""
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True))
    calls = []
    monkeypatch.setattr(gate_watch.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert calls == []
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is True


def test_check_once_alerts_on_gate_lifted(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        gate_watch._store_blocked(cur, True)
        conn.commit()
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(False, source="ручная правка"))
    calls = []
    monkeypatch.setattr(gate_watch.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert len(calls) == 1
    (chat_id, text), kwargs = calls[0]
    assert chat_id == gate_watch.CHAT_ID
    assert "СНЯТ" in text
    assert "ручная правка" in text
    assert kwargs.get("parse_mode") == "HTML"
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is False


def test_check_once_alerts_on_gate_restored(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        gate_watch._store_blocked(cur, False)
        conn.commit()
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True, source="МРТ"))
    calls = []
    monkeypatch.setattr(gate_watch.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert len(calls) == 1
    (chat_id, text), kwargs = calls[0]
    assert "снова активен" in text
    assert "МРТ" in text


def test_check_once_no_alert_when_state_unchanged(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        gate_watch._store_blocked(cur, True)
        conn.commit()
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True))
    calls = []
    monkeypatch.setattr(gate_watch.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert calls == []
