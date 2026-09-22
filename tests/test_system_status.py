"""app/system_status.py — сборка ответа страницы «Настройки» (/dashboard/system-status).

Юниты проверяют: форму ответа, fail-safe секций (degraded, а не падение) и два
инварианта реестров — список секретов обязан совпадать с проверками run.sh, а
ключи циклов — с именами, которые реально отмечаются в коде (mark_run /
alert_on_failure). Обе сверки ловят дрейф: добавили секрет в run.sh или цикл в
main._STARTUP_TASKS — тест упадёт и напомнит обновить реестр страницы.
"""
import re
from pathlib import Path

from app import system_status
from app.db import get_conn

CARD_SERVICE = Path(__file__).resolve().parent.parent


def test_secret_names_match_run_sh():
    run_sh = (CARD_SERVICE / "run.sh").read_text(encoding="utf-8")
    required = re.findall(r':\s*"\$\{([A-Z_0-9]+):\?', run_sh)
    assert required, "не нашёл проверок секретов в run.sh"
    assert required == system_status.SECRET_NAMES


def test_loops_registry_names_are_marked_in_code():
    """Каждый ключ LOOPS должен реально отмечаться в исходниках — через
    mark_run("<key>" либо alert_on_failure("<key>") (ошибки пишет
    scheduler_alert → run_log.mark_error)."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in (CARD_SERVICE / "app").rglob("*.py"))
    for spec in system_status.LOOPS:
        key = spec["key"]
        marked = ('mark_run("%s"' % key) in text or ('alert_on_failure("%s"' % key) in text
        assert marked, "цикл %s нигде не отмечается (ни mark_run, ни alert_on_failure)" % key


def test_build_returns_full_shape(monkeypatch):
    """Форма ответа и независимость секций: читающие прод-таблицы секции в
    юните глушим (freshness/nightly), остальные должны собраться на card_test."""
    monkeypatch.setattr(system_status, "_freshness", lambda cur: [])
    monkeypatch.setattr(system_status, "_nightly", lambda cur: [])
    with get_conn() as conn, conn.cursor() as cur:
        out = system_status.build(cur)

    assert set(out) >= {"ts", "money", "host", "peak24", "loops", "freshness", "nightly", "issues", "config"}
    assert len(out["loops"]) == len(system_status.LOOPS)
    assert out["host"] is not None and out["host"]["cores"] >= 1
    assert out["config"]["secrets_total"] == len(system_status.SECRET_NAMES)
    assert out["money"] is not None


def test_section_failure_is_degraded_not_crash(monkeypatch):
    """Сбой одной секции не роняет страницу: отдаём None в её поле."""
    def boom(cur):
        raise RuntimeError("таблица недоступна")

    monkeypatch.setattr(system_status, "_money", boom)
    monkeypatch.setattr(system_status, "_freshness", lambda cur: [])
    monkeypatch.setattr(system_status, "_nightly", lambda cur: [])
    with get_conn() as conn, conn.cursor() as cur:
        out = system_status.build(cur)
    assert out["money"] is None
    assert out["loops"] and out["config"]


def test_build_includes_timezone_block(monkeypatch):
    """Фаза 3: блок часового пояса в ответе страницы (текущая/домашняя зона)."""
    monkeypatch.setattr(system_status, "_freshness", lambda cur: [])
    monkeypatch.setattr(system_status, "_nightly", lambda cur: [])
    with get_conn() as conn, conn.cursor() as cur:
        out = system_status.build(cur)
    tz = out["timezone"]
    assert tz and tz["current_tz"] and tz["home_tz"]
    assert tz["is_travelling"] in (True, False)
    assert len(tz["local_time"]) == 5      # HH:MM
    assert isinstance(tz["examples"], list) and tz["examples"]


def test_money_includes_llm_usage(monkeypatch):
    """«Полные расходы» (2026-09-22): сумма agent_step (доктор) + llm_usage
    (советник/регистратор/дневник/прочее)."""
    from app import llm_usage
    monkeypatch.setattr(system_status, "_freshness", lambda cur: [])
    monkeypatch.setattr(system_status, "_nightly", lambda cur: [])
    llm_usage.record("test_module", "m", {"cost": 0.5})
    with get_conn() as conn, conn.cursor() as cur:
        out = system_status.build(cur)
    p = out["money"]["providers"][0]
    assert p["week"] >= 0.5 and p["today"] >= 0.5
    assert "учтены все вызовы" in out["money"]["uncovered"]


def test_fmt_moment_utc_and_dates(monkeypatch):
    from datetime import date, datetime, timezone
    name, age = system_status._fmt_moment(datetime.now(timezone.utc))
    assert name.startswith("сегодня")
    assert age is not None and age < 0.5
    # date-объект трактуется как день в зоне человека
    name, age = system_status._fmt_moment(date.today())
    assert name is not None
    # None и мусор не падают
    assert system_status._fmt_moment(None) == (None, None)
    assert system_status._fmt_moment("не дата вовсе")[0] == "не дата вовсе"


def test_nightly_backup_reflects_state(monkeypatch):
    """Ночная секция честно отражает состояние пинга бэкапа (ок → ok, нет строки → warn)."""
    class FakeCur:
        def __init__(self, backup):
            self._backup = backup
            self._i = 0
        def execute(self, sql, params=None):
            self._i += 1
        def fetchone(self):
            return self._backup
        def fetchall(self):
            return []

    from datetime import datetime, timezone
    ok_row = ("ok", "", "20260922_020001", 12.0)
    out = system_status._nightly(FakeCur(ok_row))
    assert out[0]["st"] == "ok" and "20260922_020001" in out[0]["v"]

    out = system_status._nightly(FakeCur(None))
    assert out[0]["st"] == "warn"

    old_row = ("ok", "", "20260920_020001", 30.0)  # пинг старше 26ч
    out = system_status._nightly(FakeCur(old_row))
    assert out[0]["st"] == "warn"


# --- _issues (2026-09-23, Шаг 2 «петли самоулучшения») ----------------------

def test_issues_returns_only_open_sorted_by_severity_then_recency():
    from app import issue_log
    from app.db import schema

    with get_conn() as conn, conn.cursor() as cur:
        issue_log.record_issue(cur, "test:sysstatus:minor", source="x", summary="мелочь", severity="minor")
        issue_log.record_issue(cur, "test:sysstatus:critical", source="y", summary="критично", severity="critical")
        issue_log.record_issue(cur, "test:sysstatus:fixed", source="z", summary="починено", severity="critical")
        issue_log.resolve_issue(cur, "test:sysstatus:fixed", resolution_ref="commit x")
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            out = system_status._issues(cur)
        keys = {r["source"] for r in out}
        assert "y" in keys and "x" in keys
        assert "z" not in keys  # fixed — не показываем
        assert out[0]["source"] == "y"  # critical раньше minor
        assert out[0]["occurrences"] == 1
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {schema()}.issue_log WHERE natural_key LIKE 'test:sysstatus:%'")
            conn.commit()
