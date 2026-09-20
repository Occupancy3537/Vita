"""app/main.py::_start_background_schedulers — премортем-фикс (2026-09-20,
проблема #3 "цепочка флагов"): раньше это была цепочка `if not FLAG: return`,
где один невыставленный флаг у начала списка молча гасил всё, что после
него. Теперь каждый пункт _STARTUP_TASKS независим — эти тесты фиксируют
именно это свойство, чтобы цепочка не вернулась незаметно при следующей
правке."""
import app.main as main


def test_startup_tasks_are_independent_not_chained(monkeypatch):
    """Ключевая регрессия премортема: флаг в начале списка выключен, но все
    флаги ПОСЛЕ него всё равно должны сработать (в старой цепочке — нет)."""
    started = []
    monkeypatch.setattr(
        main, "_STARTUP_TASKS",
        [
            ("FIRST_FLAG", lambda: started.append("first"), "first"),
            ("SECOND_FLAG", lambda: started.append("second"), "second"),
            ("THIRD_FLAG", lambda: started.append("third"), "third"),
        ],
    )
    monkeypatch.delenv("FIRST_FLAG", raising=False)  # первый выключен...
    monkeypatch.setenv("SECOND_FLAG", "1")
    monkeypatch.setenv("THIRD_FLAG", "1")

    # запускаем синхронно вместо потоков, чтобы не гоняться за таймингом треда
    def _sync_thread(target, daemon, name):
        target()
        class _T:
            def start(self_inner):
                pass
        return _T()
    monkeypatch.setattr(main.threading, "Thread", _sync_thread)

    main._start_background_schedulers()

    assert "first" not in started
    assert "second" in started
    assert "third" in started  # старая цепочка это бы никогда не запустила


def test_startup_task_failure_does_not_block_the_rest(monkeypatch):
    started = []

    def boom():
        raise RuntimeError("сбой одного планировщика")

    monkeypatch.setattr(
        main, "_STARTUP_TASKS",
        [
            ("A_FLAG", boom, "a"),
            ("B_FLAG", lambda: started.append("b"), "b"),
        ],
    )
    monkeypatch.setenv("A_FLAG", "1")
    monkeypatch.setenv("B_FLAG", "1")

    def _sync_thread(target, daemon, name):
        class _T:
            def start(self_inner):
                target()
        return _T()
    monkeypatch.setattr(main.threading, "Thread", _sync_thread)

    main._start_background_schedulers()  # не должно упасть наружу

    assert started == ["b"]


def test_startup_skips_all_when_no_flags_set(monkeypatch):
    started = []
    monkeypatch.setattr(
        main, "_STARTUP_TASKS",
        [("ONLY_FLAG", lambda: started.append("x"), "x")],
    )
    monkeypatch.delenv("ONLY_FLAG", raising=False)
    main._start_background_schedulers()
    assert started == []
