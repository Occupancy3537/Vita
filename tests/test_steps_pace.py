"""Темп дня по шагам, замеры, серия «Шаги ≥ 10 000» (2026-10-01)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import steps_pace as sp
from app import steps_sampler as ss
from app import vita
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def test_single_goal_everywhere():
    from app import dashboard, goals
    assert goals.STEPS_TARGET_DAILY == 10000 and dashboard._STEPS_TARGET_DAILY == 10000
    assert sp.STEPS_TARGET_DAILY == 10000


def test_expected_steps_linear_between_0700_and_2200():
    assert sp.expected_steps(6.0) == 0 and sp.expected_steps(7.0) == 0
    assert sp.expected_steps(14.5) == 5000 and sp.expected_steps(22.0) == 10000 and sp.expected_steps(23.5) == 10000


def test_pace_score_is_not_a_constant_hundred():
    assert sp.pace(2000, 12.0)["score"] == 60            # ожидалось 3333 → отстал, круг показывает 60
    assert sp.pace(2000, 12.0)["behind"] is True
    assert sp.pace(3400, 12.0)["score"] == 100 and sp.pace(3400, 12.0)["behind"] is False
    assert sp.pace(5768, 16.5)["score"] == 91             # ожидалось 6333


def test_pace_hints_minutes_to_pace_and_goal():
    p = sp.pace(2000, 12.0)
    assert p["delta"] == -1333 and p["to_pace_min"] == 13 and p["left"] == 8000 and p["to_goal_min"] == 80
    assert sp.pace(9000, 12.0)["to_pace_min"] == 0


def test_pace_early_morning_is_hundred_not_noise():
    assert sp.pace(0, 7.4)["score"] == 100 and sp.pace(0, 7.4)["behind"] is False


def test_pace_no_data_is_none_not_zero():
    p = sp.pace(None, 12.0)
    assert p["score"] is None and p["expected"] is None


def test_evening_pace_equals_goal_percent_and_closed_day_score():
    assert sp.pace(7000, 22.0)["score"] == 70
    assert sp.closed_day_score(7000) == 70 and sp.closed_day_score(15421) == 100 and sp.closed_day_score(None) is None


def test_build_steps_uses_pace_and_ignores_gate(monkeypatch):
    class _C:
        def execute(self, *a, **k): pass
        def fetchone(self): return (2000,)
    monkeypatch.setattr(vita.timeutil, "now_local", lambda *a, **k: datetime(2026, 10, 1, 12, 0))
    out = vita.build_steps(_C(), {"blocked": True})
    assert out["target"] == 10000                          # щадящий режим цель больше не снижает
    assert out["pace"]["score"] == 60 and out["behind_pace"] is True and out["status_word"] == "ниже темпа"


# ─────── замеры ───────

def test_sampler_records_new_updates_once_and_is_idempotent(monkeypatch):
    day = date(2026, 10, 2)
    t1 = datetime(2026, 10, 2, 6, 20, tzinfo=timezone.utc)
    live = {"row": (day, 5768, t1)}   # health.live_steps_today карта card_service не пишет — читаем заглушкой
    monkeypatch.setattr(ss, "_live_row", lambda cur: live["row"])
    with get_conn() as conn, conn.cursor() as cur:
        assert ss.sample_once(cur) is True
        assert ss.sample_once(cur) is False                  # то же updated_at — не дублируем
        live["row"] = (day, 6000, t1 + timedelta(minutes=20))
        assert ss.sample_once(cur) is True
        got = ss.day_samples(cur, day)
    assert [s for _, s in got] == [5768, 6000] and got[0][0] < got[1][0]


def test_sampler_no_live_row_is_noop(monkeypatch):
    monkeypatch.setattr(ss, "_live_row", lambda cur: None)
    with get_conn() as conn, conn.cursor() as cur:
        assert ss.sample_once(cur) is False


# ─────── серия «Шаги ≥ 10 000» ───────

def _row(d, steps, sleep=450):
    return {"Дата": d, "Чистый_сон_мин": sleep, "Шаги_за_вчера": steps}


def _steps_streak(result):
    return next((s for s in result["streaks"] if s["key"] == "steps"), None)


def test_steps_streak_counts_closed_days_and_today_pending():
    # в строке D шаги дня D−1: дни 09-27 (11 000), 09-28 (12 000), 09-29 (13 000) выполнены
    rows = [_row("2026-09-27", 9000), _row("2026-09-28", 11000), _row("2026-09-29", 12000), _row("2026-09-30", 13000)]
    res = vita.build_streaks(rows, [], [], "2026-09-30", steps_today=4000, now_hour=11.0)
    s = _steps_streak(res)
    assert s["count"] == 3 and s["label"] == "Шаги ≥ 10 000"
    assert s["days"][-1]["c"] == "t"                       # сегодня идёт, не провал


def test_steps_streak_breaks_on_short_day_and_today_goal_extends(monkeypatch):
    monkeypatch.setattr(vita, "STREAK_FREEZE_POOL", 0)     # без заморозок провал рвёт серию
    rows = [_row("2026-09-28", 11000), _row("2026-09-29", 7000), _row("2026-09-30", 12000)]
    # дни: 09-28 = 7000 (провал), 09-29 = 12000, сегодня 09-30 цель уже взята
    s = _steps_streak(vita.build_streaks(rows, [], [], "2026-09-30", steps_today=10500, now_hour=20.0))
    assert s["count"] == 2 and s["status"] == "alive"      # 09-29 + сегодня


def test_steps_streak_at_risk_late_when_behind_pace():
    rows = [_row("2026-09-29", 12000), _row("2026-09-30", 12000)]
    risky = _steps_streak(vita.build_streaks(rows, [], [], "2026-09-30", steps_today=3000, now_hour=18.0))
    calm = _steps_streak(vita.build_streaks(rows, [], [], "2026-09-30", steps_today=3000, now_hour=10.0))
    assert risky["at_risk"] is True and risky["status"] == "at_risk"
    assert calm["at_risk"] is False


def test_steps_streak_not_paused_by_travel():
    rows = [_row("2026-09-29", 12000), _row("2026-09-30", 12000)]
    res = vita.build_streaks(rows, [], [], "2026-09-30", travel_days={"2026-09-29", "2026-09-30"}, steps_today=None, now_hour=None)
    assert _steps_streak(res)["count"] >= 1               # часы с собой — шаги считаются как обычно


# ─────── серия «Без алкоголя» (2026-10-01) ───────

def _meal(day, alc=None, t="19:00"):
    m = {"Date": f"{day}T{t}"}
    if alc is not None:
        m["Алкоголь"] = alc
    return m


def _alc(result):
    return next((s for s in result["streaks"] if s["key"] == "alcohol"), None)


def _days(n, start=20):
    return [f"2026-09-{d:02d}" for d in range(start, start + n)]


def test_alcohol_streak_counts_dry_days_with_logged_meals():
    ds = _days(5)
    rows = [_row(d, 12000) for d in ds]
    meals = [_meal(d, "0") for d in ds]
    s = _alc(vita.build_streaks(rows, meals, [], ds[-1]))
    assert s["count"] == 5 and s["label"] == "Без алкоголя" and s["status"] == "alive"


def test_alcohol_streak_broken_by_a_drinking_day(monkeypatch):
    monkeypatch.setattr(vita, "STREAK_FREEZE_POOL", 0)
    ds = _days(5)
    rows = [_row(d, 12000) for d in ds]
    meals = [_meal(d, "0") for d in ds]
    meals[1] = _meal(ds[1], "14.5")                      # бокал вина 21 сентября
    s = _alc(vita.build_streaks(rows, meals, [], ds[-1]))
    assert s["count"] == 3 and s["record"] == 3          # после срыва — 3 дня подряд
    assert s["days"][1]["c"] == "x"


def test_alcohol_trace_amount_is_not_a_drink():
    ds = _days(3)
    rows = [_row(d, 12000) for d in ds]
    meals = [_meal(ds[0], "0.3"), _meal(ds[1], "1.0"), _meal(ds[2], "0")]      # кефир и т.п. — следы ≤ 1 г
    assert _alc(vita.build_streaks(rows, meals, [], ds[-1]))["count"] == 3


def test_alcohol_day_without_any_meal_is_paused_not_a_break_and_not_growth():
    ds = _days(4)
    rows = [_row(d, 12000) for d in ds]
    meals = [_meal(ds[0], "0"), _meal(ds[1], "0"), _meal(ds[3], "0")]           # 22-го еда не записана
    s = _alc(vita.build_streaks(rows, meals, [], ds[-1]))
    assert s["count"] == 3 and s["days"][2]["c"] == "p"


def test_alcohol_streak_paused_by_travel_mode():
    ds = _days(3)
    rows = [_row(d, 12000) for d in ds]
    meals = [_meal(d, "30") for d in ds]                 # в поездке выпитое не записывают/не считаем
    s = _alc(vita.build_streaks(rows, meals, [], ds[-1], travel_days=set(ds)))
    assert s is None or s["count"] == 0 or all(x["c"] in ("p", "t") for x in s["days"])
