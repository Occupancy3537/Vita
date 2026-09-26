"""app/lab_reminder.py — напоминание «панель созревает через 3 дня» (тикет
«оптимизатор сдачи анализов», 2026-09-26, Часть 3.3)."""
from datetime import date, datetime, timedelta, timezone

from app import lab_reminder
from app.db import get_conn, schema

TODAY = date(2026, 9, 26)


def _fake_now(d):
    class _N:
        def date(self):
            return d
    return _N()


def _seed_lab_result(cur, code, ts_event):
    from ulid import ULID
    cur.execute(
        f"INSERT INTO {schema()}.lab_result (id, ts_event, provenance, marker_key, value_num) "
        "VALUES (%s, %s, '{}', %s, 1)",
        (f"lrt_{ULID()}", ts_event, code),
    )


def test_run_once_sends_one_reminder_for_panel_within_window(monkeypatch):
    monkeypatch.setattr(lab_reminder.timeutil, "now_local", lambda: _fake_now(TODAY))
    calls = []
    monkeypatch.setattr(lab_reminder.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    with get_conn() as conn, conn.cursor() as cur:
        # M004 просрочен -> due=today (панель "сегодня", в окне 0..3 дня)
        _seed_lab_result(cur, "M004", datetime(2024, 1, 1, tzinfo=timezone.utc))
        conn.commit()
    out = lab_reminder.run_once()
    assert out["sent"] == 1
    assert len(calls) == 1
    (source, priority, text), kwargs = calls[0]
    assert source == "lab_reminder" and priority == "normal"
    assert "Креатинин" in text


def test_run_once_does_not_resend_same_panel_date_twice(monkeypatch):
    monkeypatch.setattr(lab_reminder.timeutil, "now_local", lambda: _fake_now(TODAY))
    calls = []
    monkeypatch.setattr(lab_reminder.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    with get_conn() as conn, conn.cursor() as cur:
        _seed_lab_result(cur, "M004", datetime(2024, 1, 1, tzinfo=timezone.utc))
        conn.commit()
    lab_reminder.run_once()
    lab_reminder.run_once()
    assert len(calls) == 1  # второй прогон — та же дата панели, уже отправляли


def test_run_once_silent_when_panel_far_in_future(monkeypatch):
    """Панель за пределами окна 0..3 дня — молчим (подменяем generate_plan
    целиком: пустая card_test сама по себе НЕ даёт "нечего сдавать" — при
    пустой истории все стоящие правила каталога считаются просроченными
    "сейчас", это ожидаемо и проверено другими тестами; здесь проверяем
    именно окно 0..3 дня в изоляции от состояния каталога)."""
    monkeypatch.setattr(lab_reminder.timeutil, "now_local", lambda: _fake_now(TODAY))
    calls = []
    monkeypatch.setattr(lab_reminder.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(lab_reminder, "generate_plan", lambda cur, today=None: {
        "panels": [{"date": (TODAY + timedelta(days=30)).isoformat(),
                    "export_text": "далёкая панель"}],
    })
    out = lab_reminder.run_once()
    assert out["sent"] == 0
    assert calls == []
