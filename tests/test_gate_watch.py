"""app/gate_watch.py — алерт на снятие/возврат гейта нагрузки (порт из n8n
today-dashboard Build Today JSON, 2026-09-20). health.gate_state — реальная
прод-таблица (1 строка-синглтон, safety-критичная: тот источник, с которым
сверяется алерт о снятии/возврате гейта при активной грыже L5/S1).

2026-09-24 (ROADMAP 0.7): раньше фикстура ПРОСТО удаляла боевую строку
до/после каждого теста, без сохранения исходного значения — тот же класс
бага, что уже стоил инцидентов #54 (health.anomaly_log) и #60
(health.backup_alert_state), просто этот случай ещё не успел выстрелить
(живой планировщик тикает раз в 15 мин и обычно успевает переписать
правильное значение раньше, чем это стало бы заметно — но полагаться на
удачное совпадение по времени для safety-таблицы нельзя). Теперь —
`_isolate_real_schema_writes` (tests/conftest.py): соединение с боевой
базой физически не может закоммитить ничего, что бы тест ни исполнил,
поэтому реальная health.gate_state вообще не видит эти DELETE/INSERT."""
import pytest

from app import gate_watch
from app.db import get_conn

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


@pytest.fixture(autouse=True)
def reset_gate_state(_isolate_real_schema_writes):
    """DELETE здесь — не "уборка на всякий случай", а часть логики теста:
    test_check_once_no_alert_on_first_ever_run проверяет поведение именно
    при ОТСУТСТВИИ строки. Под _isolate_real_schema_writes это безопасно —
    ничего не коммитится, реальная строка не видит этот DELETE вообще."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.gate_state WHERE id = 1")
        conn.commit()
    yield


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
    """Нет строки в gate_state И прошлых прогонов в scheduler_run_log нет —
    настоящий первый запуск, не алертим (тихая инициализация сохранена)."""
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True))
    monkeypatch.setattr(gate_watch.run_log, "last_ok_at", lambda name: None)
    calls = []
    issue_calls = []
    monkeypatch.setattr(gate_watch.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(gate_watch.issue_log, "record_issue", lambda *a, **kw: issue_calls.append((a, kw)))
    gate_watch.check_once()
    assert calls == []
    assert issue_calls == []
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is True


def test_check_once_alerts_when_state_row_lost_after_past_runs(monkeypatch):
    """Часть 2 тикета «хвост» (2026-09-26): нет строки в gate_state, НО цикл
    раньше успешно работал (scheduler_run_log.last_ok_at не пуст) — это не
    первый запуск, состояние потерялось. Критический алерт + issue_log,
    честный текст с окном X-Y, потом всё равно тихо инициализируем текущим."""
    from datetime import datetime, timezone
    last_ok = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True, source="МРТ"))
    monkeypatch.setattr(gate_watch.run_log, "last_ok_at", lambda name: last_ok)
    calls = []
    issue_calls = []
    monkeypatch.setattr(gate_watch.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(gate_watch.issue_log, "record_issue", lambda *a, **kw: issue_calls.append((a, kw)))
    gate_watch.check_once()

    assert len(calls) == 1
    (source, priority, text), kwargs = calls[0]
    assert source == "gate_watch" and priority == "critical"
    assert "потеряно" in text.lower()
    assert "2026-09-26t10:00" in text.lower()  # начало окна — последний известный прогон
    assert kwargs.get("parse_mode") == "HTML"

    assert len(issue_calls) == 1
    (cur_arg, natural_key, src, summary), kw2 = issue_calls[0]
    assert natural_key == "gate_watch_state_lost"
    assert kw2.get("severity") == "critical"

    # тихо инициализировали текущим состоянием после алерта, как раньше
    with get_conn() as conn, conn.cursor() as cur:
        assert gate_watch._last_known_blocked(cur) is True


def test_check_once_alerts_on_gate_lifted(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        gate_watch._store_blocked(cur, True)
        conn.commit()
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(False, source="ручная правка"))
    calls = []
    monkeypatch.setattr(gate_watch.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert len(calls) == 1
    (source, priority, text), kwargs = calls[0]
    assert source == "gate_watch" and priority == "critical"
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
    monkeypatch.setattr(gate_watch.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert len(calls) == 1
    (source, priority, text), kwargs = calls[0]
    assert "снова активен" in text
    assert "МРТ" in text


def test_check_once_no_alert_when_state_unchanged(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        gate_watch._store_blocked(cur, True)
        conn.commit()
    monkeypatch.setattr(gate_watch, "get_today_dashboard", lambda cur: _fake_today(True))
    calls = []
    monkeypatch.setattr(gate_watch.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    gate_watch.check_once()
    assert calls == []
