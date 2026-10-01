"""app/health_watchdog.py — порт n8n Health Watchdog (2026-09-20, группа 2).
Юниты на чистые хелперы + FakeCursor для detect() (health.* — прод-схема, тот
же принцип, что test_doctor_context.py); state-таблицы (watchdog_nudged/
watchdog_state) реальные — сбрасываем тестовые ключи до/после."""
import pytest
from datetime import datetime, timedelta

from app import health_watchdog as hw
from app.db import get_conn

# «свежий» визит — относительная дата: фиксированная дата ломала тест через 30 дней (2026-10-01)
RECENT_VISIT = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


class FakeCursor:
    def __init__(self, queue):
        self.queue = list(queue)
        self._current = []

    def execute(self, query, params=None):
        self._current = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


# --- чистые хелперы -----------------------------------------------------------

def test_num_parses_comma_and_spaces():
    assert hw._num("24,3") == 24.3
    assert hw._num("1 234,5") == 1234.5
    assert hw._num("") is None
    assert hw._num(None) is None
    assert hw._num("не число") is None


def test_d10_handles_both_date_formats():
    assert hw._d10("15.06.2026") == "2026-06-15"
    assert hw._d10("2026-06-15") == "2026-06-15"
    assert hw._d10(None) == ""


def test_days_computes_signed_difference():
    assert hw._days("2026-09-20", "2026-09-10") == 10.0
    assert hw._days("2026-09-10", "2026-09-20") == -10.0


# --- watchdog_nudged / watchdog_state (реальные таблицы) --------------------

TEST_KEY = "test:health_watchdog_key"


def test_store_and_load_nudge_roundtrip():
    with get_conn() as conn, conn.cursor() as cur:
        hw._store_state(cur, {TEST_KEY: "2026-09-20"}, None, None)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        nudged, _, _ = hw._load_state(cur)
    assert nudged.get(TEST_KEY) == "2026-09-20"


def test_store_state_upserts_last_reviewed_visit_without_clobbering_read_fail():
    with get_conn() as conn, conn.cursor() as cur:
        hw._store_state(cur, {}, "V001", "2026-09-01")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        hw._store_state(cur, {}, "V002", None)  # обновляем только визит, notice не трогаем
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        _, last_visit, last_notice = hw._load_state(cur)
    assert last_visit == "V002"
    assert last_notice == "2026-09-01"


# --- detect() на FakeCursor: только новая сдача крови (needs_lab_review) ----

# порядок колонок должен точно совпадать с SELECT'ами в _fetch_sources —
# FakeCursor отдаёт tuple-строки как настоящий psycopg-курсор, не dict'ы.
_COLS = {
    "results": ("Visit_ID", "Marker_ID", "Value", "Lab_Min", "Lab_Max", "Original_Unit"),
    "markers": ("Marker_ID", "Name"),
    "visits": ("Visit_ID", "Date"),
    "sym_rows": ("Symptom_ID", "Date", "Symptom", "Severity", "Change", "Status"),
    "inv_rows": ("Inv_ID", "Status", "Updated", "Opened"),
    "day_sum": ("Date", "Добавленный сахар", "Насыщенные жиры", "Клетчатка", "Calories"),
    "daily": ("Дата", "ВСР_ночная", "Пульс_ночной_средний", "Чистый_сон_мин", "Тренировка_Ккал", "Лекарства_принимаемые"),
    "meds_rows": ("Med_ID", "Name", "Class", "Dose", "Status", "Started", "Stopped", "Reason"),
    "pstate_rows": ("Status", "Contra_Other", "Note"),
    "lab_plan": ("Test", "Status", "Next_Due"),
}
_ORDER = ["results", "markers", "visits", "sym_rows", "inv_rows", "day_sum", "daily", "meds_rows", "pstate_rows", "lab_plan"]


def _rows_to_tuples(name, dicts):
    cols = _COLS[name]
    return [tuple(d.get(c) for c in cols) for d in dicts]


def _empty_detect_queue(**overrides):
    """10 запросов в _fetch_sources, в порядке вызова — каждый набор строк как
    список dict'ов (удобно писать в тесте), конвертируется в tuple-строки."""
    base = {k: [] for k in _ORDER}
    base.update(overrides)
    return [_rows_to_tuples(name, base[name]) for name in _ORDER]


class FakeCursorWithState(FakeCursor):
    """detect() сначала зовёт _fetch_sources (10 запросов), затем _load_state
    (ещё 2: nudged-таблица целиком, потом state-синглтон)."""
    def __init__(self, fetch_queue, nudged_rows=None, state_row=None):
        super().__init__(fetch_queue + [nudged_rows or [], [state_row] if state_row else []])


def test_detect_fires_on_new_visit_with_two_markers():
    visits = [{"Visit_ID": "V1", "Date": RECENT_VISIT}]
    results = [
        {"Visit_ID": "V1", "Marker_ID": "M1", "Value": "10", "Lab_Min": "4", "Lab_Max": "6", "Original_Unit": ""},
        {"Visit_ID": "V1", "Marker_ID": "M2", "Value": "5", "Lab_Min": "1", "Lab_Max": "9", "Original_Unit": ""},
    ]
    markers = [{"Marker_ID": "M1", "Name": "Глюкоза"}, {"Marker_ID": "M2", "Name": "Альбумин"}]
    cur = FakeCursorWithState(_empty_detect_queue(visits=visits, results=results, markers=markers))
    d = hw.detect(cur)
    assert d["needs_lab_review"] is True
    assert d["fire"] is True
    assert "Глюкоза" in d["newest_markers"]
    assert d["newest_markers"]["Глюкоза"]["value"] == 10.0


def test_detect_flags_out_of_range_marker():
    visits = [{"Visit_ID": "V1", "Date": RECENT_VISIT}]
    results = [
        {"Visit_ID": "V1", "Marker_ID": "M1", "Value": "10", "Lab_Min": "4", "Lab_Max": "6", "Original_Unit": ""},
        {"Visit_ID": "V1", "Marker_ID": "M2", "Value": "5", "Lab_Min": "1", "Lab_Max": "9", "Original_Unit": ""},
    ]
    markers = [{"Marker_ID": "M1", "Name": "Глюкоза"}, {"Marker_ID": "M2", "Name": "Альбумин"}]
    cur = FakeCursorWithState(_empty_detect_queue(visits=visits, results=results, markers=markers))
    d = hw.detect(cur)
    names = {a["name"] for a in d["abnormal"]}
    assert "Глюкоза" in names  # 10 > Lab_Max 6


def test_detect_no_fire_when_already_reviewed():
    visits = [{"Visit_ID": "V1", "Date": RECENT_VISIT}]
    results = [
        {"Visit_ID": "V1", "Marker_ID": "M1", "Value": "5", "Lab_Min": "4", "Lab_Max": "6", "Original_Unit": ""},
        {"Visit_ID": "V1", "Marker_ID": "M2", "Value": "5", "Lab_Min": "1", "Lab_Max": "9", "Original_Unit": ""},
    ]
    markers = [{"Marker_ID": "M1", "Name": "Глюкоза"}, {"Marker_ID": "M2", "Name": "Альбумин"}]
    cur = FakeCursorWithState(_empty_detect_queue(visits=visits, results=results, markers=markers),
                              state_row=("V1", None))
    d = hw.detect(cur)
    assert d["needs_lab_review"] is False
    # (не проверяем общий d["fire"] — тут нарочно пустые Daily_Trends/Symptom_Log/
    # Patient_State, это триггерит отдельный сигнал notify_read_failure, а не баг)


def test_detect_symptom_worsening_triggers_nudge():
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    sym_rows = [{"Symptom_ID": "s1", "Date": today, "Symptom": "боль в спине", "Severity": "5",
                 "Change": "усилилась", "Status": "active"}]
    cur = FakeCursorWithState(_empty_detect_queue(sym_rows=sym_rows))
    d = hw.detect(cur)
    assert d["should_nudge"] is True
    assert len(d["new_sym_alerts"]) == 1


def test_detect_suppresses_nudge_when_open_investigation():
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    sym_rows = [{"Symptom_ID": "s1", "Date": today, "Symptom": "боль в спине", "Severity": "5",
                 "Change": "усилилась", "Status": "active"}]
    inv_rows = [{"Inv_ID": "inv1", "Status": "open", "Updated": today, "Opened": today}]
    cur = FakeCursorWithState(_empty_detect_queue(sym_rows=sym_rows, inv_rows=inv_rows))
    d = hw.detect(cur)
    assert d["open_investigation"] is True
    assert d["should_nudge"] is False


def test_detect_already_nudged_symptom_not_repeated():
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    sym_rows = [{"Symptom_ID": "s1", "Date": today, "Symptom": "боль в спине", "Severity": "5",
                 "Change": "усилилась", "Status": "active"}]
    key = f"s:s1:{today}"
    cur = FakeCursorWithState(_empty_detect_queue(sym_rows=sym_rows), nudged_rows=[(key, today)])
    d = hw.detect(cur)
    assert d["new_sym_alerts"] == []
    assert d["should_nudge"] is False


def test_detect_read_failure_detected_when_critical_tables_empty():
    cur = FakeCursorWithState(_empty_detect_queue())
    d = hw.detect(cur)
    assert "Daily_Trends" in d["read_failures"]
    assert "Visits" in d["read_failures"]
    assert d["notify_read_failure"] is True


def test_detect_read_failure_not_renotified_within_3_days():
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc) + timedelta(hours=10) - timedelta(days=1)).strftime("%Y-%m-%d")
    cur = FakeCursorWithState(_empty_detect_queue(), state_row=(None, yesterday))
    d = hw.detect(cur)
    assert d["notify_read_failure"] is False


# --- build_prompt --------------------------------------------------------------

def test_build_prompt_includes_lab_review_section():
    d = {
        "today": "2026-09-20", "needs_lab_review": True,
        "newest_visit": {"id": "V1", "date": "2026-09-01"}, "prev_visit_date": "2026-06-01",
        "newest_markers": {"Глюкоза": {"value": 6.5, "lo": 4.0, "hi": 6.0, "unit": ""}},
        "prev_markers": {}, "nutrition_period": {"days": 5}, "wellness_period": {"days": 5},
        "medications": {"active": [], "changed_near_visit": []}, "abnormal": [],
        "new_abnormal": [], "new_sym_alerts": [], "new_overdue": [], "stale_thread": None,
        "stale_investigations": [], "notify_read_failure": False, "read_failures": [],
    }
    text = hw.build_prompt(d)
    assert "РАЗБОР СВЕЖЕЙ СДАЧИ КРОВИ" in text
    assert "Глюкоза" in text


def test_build_prompt_spine_topic_asks_two_numbers():
    d = {
        "today": "2026-09-20", "needs_lab_review": False, "newest_visit": None, "prev_visit_date": None,
        "newest_markers": {}, "prev_markers": {}, "nutrition_period": {}, "wellness_period": {},
        "medications": {}, "abnormal": [], "new_abnormal": [], "new_sym_alerts": [], "new_overdue": [],
        "stale_thread": {"symptom": "грыжа L5/S1", "status": "active", "age_days": 15, "last_date": "2026-09-05"},
        "stale_investigations": [], "notify_read_failure": False, "read_failures": [],
    }
    text = hw.build_prompt(d)
    assert "метров" in text and "минут" in text


# --- run_once / call_model (мокаем HTTP и Telegram) -------------------------

def test_call_model_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert hw.call_model("тест") == ""


def test_run_once_no_fire_sends_nothing(monkeypatch):
    monkeypatch.setattr(hw, "detect", lambda cur: {"fire": False, "_new_last_read_fail_notice": None})
    calls = []
    monkeypatch.setattr(hw.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    hw.run_once()
    assert calls == []


def test_run_once_fires_sends_telegram_and_marks_state(monkeypatch):
    d = {
        "fire": True, "needs_lab_review": False, "newest_visit": None, "new_abnormal": [],
        "new_sym_alerts": [{"key": TEST_KEY, "symptom": "x", "severity": 5, "change": "усилилась", "id": "s1"}],
        "new_overdue": [], "stale_thread": None, "today": "2026-09-20", "_new_last_read_fail_notice": None,
    }
    monkeypatch.setattr(hw, "detect", lambda cur: d)
    monkeypatch.setattr(hw, "call_model", lambda prompt: "текст уведомления")
    calls = []
    monkeypatch.setattr(hw.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    hw.run_once()
    assert len(calls) == 1
    assert calls[0][0] == ("health_watchdog", "normal", "текст уведомления")
    with get_conn() as conn, conn.cursor() as cur:
        nudged, _, _ = hw._load_state(cur)
    assert nudged.get(TEST_KEY) == "2026-09-20"
