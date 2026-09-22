"""app/run_log.py — журнал прогонов фоновых циклов (2026-09-22, страница
«Настройки»): у циклов не было следа успешных прогонов, ошибки видны только
через alert_on_failure в Telegram."""
from app import run_log
from app.db import get_conn, schema


def _read(name):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT last_ok_at, last_error, last_error_at FROM {schema()}.scheduler_run_log WHERE name = %s",
            (name,),
        )
        return cur.fetchone()


def test_mark_run_creates_row_with_ok_and_no_error():
    run_log.mark_run("test_loop_a")
    row = _read("test_loop_a")
    assert row is not None
    assert row[0] is not None       # last_ok_at записан
    assert row[1] is None           # ошибок не было


def test_mark_error_stored_and_survives_later_success():
    run_log.mark_error("test_loop_b", RuntimeError("boom"))
    row = _read("test_loop_b")
    assert row[1] == "boom" and row[2] is not None
    run_log.mark_run("test_loop_b")
    row = _read("test_loop_b")
    assert row[0] is not None       # успех обновил last_ok_at
    assert row[1] == "boom"         # а последняя ошибка — не стёрта (история)


def test_mark_run_never_raises_when_db_unavailable(monkeypatch):
    """Наблюдаемость не должна ломать сам цикл — fail-safe, как alert_on_failure."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(run_log, "get_conn", boom)
    run_log.mark_run("test_loop_c")                    # не бросает
    run_log.mark_error("test_loop_c", ValueError("x"))  # не бросает
    assert _read("test_loop_c") is None


def test_mark_run_throttle_skips_repeat_within_interval(monkeypatch):
    """Поллеры зовут mark_run каждые ~30с — в БД пишем не чаще интервала."""
    run_log._last_write.clear()
    calls = {"n": 0}
    real = run_log.get_conn

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(run_log, "get_conn", counting)
    run_log.mark_run("test_loop_d", min_interval_seconds=300)
    run_log.mark_run("test_loop_d", min_interval_seconds=300)
    assert calls["n"] == 1


def test_alert_on_failure_writes_run_log(monkeypatch):
    """alert_on_failure (scheduler_alert.py) дополнительно пишет ошибку в журнал."""
    from app import scheduler_alert
    monkeypatch.setattr(scheduler_alert, "run_notify", lambda *a, **k: {"send": False})
    scheduler_alert.alert_on_failure("test_loop_e", RuntimeError("kaboom"))
    row = _read("test_loop_e")
    assert row is not None and row[1] == "kaboom"
