"""app/nutrition_reports.py — порт n8n `Reports` (дневной путь) + `Weekly Food
Report` (2026-09-20, группа 2). health.meals/day_sum — реальная прод-схема
(FakeCursor для detect-логики, реальные таблицы только там, где нужна
проверка записи — тестовые Entry_ID/даты, не трогаем настоящие приёмы пищи)."""
from datetime import datetime, timedelta, timezone

import pytest

from app import nutrition_reports as nr
from app.db import get_conn


# build_daily_report/build_weekly_report тестируются через monkeypatch их
# собственных _fetch_meals/_fetch_recent_meals (dict-строки, тот же формат,
# что реально отдаёт _rows_as_dicts) — не нужен отдельный FakeCursor.

# --- build_daily_report --------------------------------------------------

def test_build_daily_report_none_when_no_meals(monkeypatch):
    monkeypatch.setattr(nr, "_fetch_meals", lambda cur, since, until: [])
    assert nr.build_daily_report(None) is None


def test_build_daily_report_categorizes_by_hour_and_sums(monkeypatch):
    now_vl = datetime.now(nr.VL)
    breakfast = now_vl.replace(hour=8, minute=0, second=0, microsecond=0)
    dinner = now_vl.replace(hour=19, minute=0, second=0, microsecond=0)
    rows = [
        {"Date": breakfast, "Meal_description": "овсянка", "NOVA": "1", "Calories": "300", "Proteins": "10",
         **{f: "" for f in nr.FIELDS if f not in ("Calories", "Proteins")}},
        {"Date": dinner, "Meal_description": "чипсы", "NOVA": "4", "Calories": "200", "Proteins": "2",
         **{f: "" for f in nr.FIELDS if f not in ("Calories", "Proteins")}},
    ]
    monkeypatch.setattr(nr, "_fetch_meals", lambda cur, since, until: rows)
    d = nr.build_daily_report(None)
    assert d["Calories"] == 500
    assert d["Proteins"] == 12
    assert "овсянка" in d["Breakfast_Meals"]
    assert "чипсы" in d["Dinner_Meals"]
    assert "чипсы" in d["Ultra_Processed_Today"]
    assert d["daysTracked"] == 1


def test_build_daily_report_non_ultra_meal_not_in_ultra_list(monkeypatch):
    now_vl = datetime.now(nr.VL)
    rows = [{"Date": now_vl.replace(hour=8), "Meal_description": "овсянка", "NOVA": "1",
             **{f: "" for f in nr.FIELDS}}]
    monkeypatch.setattr(nr, "_fetch_meals", lambda cur, since, until: rows)
    d = nr.build_daily_report(None)
    assert d["Ultra_Processed_Today"] == "Нет данных"


# --- build_weekly_report --------------------------------------------------

def test_build_weekly_report_none_when_no_meals(monkeypatch):
    monkeypatch.setattr(nr, "_fetch_recent_meals", lambda cur, limit_days=60: [])
    assert nr.build_weekly_report(None) is None


def test_build_weekly_report_averages_per_day_not_per_meal(monkeypatch):
    # день3 — самый свежий (max_date) и, как в оригинале, ИСКЛЮЧАЕТСЯ из
    # усреднения (вдруг ещё не закончен) — считаем только день1+день2.
    now_vl = datetime.now(nr.VL)
    day1 = now_vl - timedelta(days=3)
    day2 = now_vl - timedelta(days=2)
    day3 = now_vl - timedelta(days=1)
    rows = [
        {"Date": day1.replace(hour=8), "Meal_description": "x", "NOVA": "1",
         "Calories": "1000", **{f: "" for f in nr.FIELDS if f != "Calories"}},
        {"Date": day1.replace(hour=19), "Meal_description": "y", "NOVA": "1",
         "Calories": "1000", **{f: "" for f in nr.FIELDS if f != "Calories"}},
        {"Date": day2.replace(hour=8), "Meal_description": "z", "NOVA": "1",
         "Calories": "1000", **{f: "" for f in nr.FIELDS if f != "Calories"}},
        {"Date": day3.replace(hour=8), "Meal_description": "исключён", "NOVA": "1",
         "Calories": "9999", **{f: "" for f in nr.FIELDS if f != "Calories"}},
    ]
    monkeypatch.setattr(nr, "_fetch_recent_meals", lambda cur, limit_days=60: rows)
    d = nr.build_weekly_report(None)
    # день1: 2000 ккал, день2: 1000 ккал -> среднее (2000+1000)/2 = 1500, не (1000*3)/3=1000
    assert d["Calories"] == 1500


def test_build_weekly_report_only_surfaces_narrow_field_set(monkeypatch):
    now_vl = datetime.now(nr.VL)
    rows = [
        {"Date": now_vl - timedelta(days=2), "Meal_description": "x", "NOVA": "1", **{f: "10" for f in nr.FIELDS}},
        {"Date": now_vl - timedelta(days=1), "Meal_description": "y", "NOVA": "1", **{f: "10" for f in nr.FIELDS}},
    ]
    monkeypatch.setattr(nr, "_fetch_recent_meals", lambda cur, limit_days=60: rows)
    d = nr.build_weekly_report(None)
    assert set(d.keys()) == set(nr.WEEKLY_SURFACED_FIELDS)
    assert "Витамин К" not in d  # известное сужение оригинала, не мой недосмотр


def test_build_weekly_report_late_night_meal_counts_as_previous_day(monkeypatch):
    """parseDateWithShift: время < 2:00 (ВЛ) -> предыдущий день. Второй (более
    свежий) приём нужен, чтобы поздний ужин не оказался единственным и не
    попал под исключение max_date."""
    now_vl = datetime.now(nr.VL)
    late_night = (now_vl - timedelta(days=2)).replace(hour=1, minute=0)
    later_meal = now_vl - timedelta(days=1)
    rows = [
        {"Date": late_night, "Meal_description": "поздний ужин", "NOVA": "1",
         "Calories": "500", **{f: "" for f in nr.FIELDS if f != "Calories"}},
        {"Date": later_meal, "Meal_description": "свежее", "NOVA": "1",
         "Calories": "1", **{f: "" for f in nr.FIELDS if f != "Calories"}},
    ]
    monkeypatch.setattr(nr, "_fetch_recent_meals", lambda cur, limit_days=60: rows)
    d = nr.build_weekly_report(None)
    assert d is not None
    assert d["Calories"] == 500  # поздний ужин ушёл на day-3 (сдвиг), later_meal — на day-2 (max_date, исключён)


# --- prompts (просто не падают и содержат ключевые данные) -----------------

def test_build_daily_prompt_includes_meal_data():
    d = {"season": "осень", "month": 9, "Breakfast_Meals": "- овсянка", "Lunch_Meals": "Нет данных",
         "Snack_Meals": "Нет данных", "Dinner_Meals": "Нет данных", "Ultra_Processed_Today": "Нет данных"}
    text = nr.build_daily_prompt(d)
    assert "овсянка" in text
    assert "Привет, Влад" in text


def test_build_weekly_prompt_includes_data():
    d = {f: 100 for f in nr.WEEKLY_SURFACED_FIELDS}
    text = nr.build_weekly_prompt(d)
    assert "неделю" in text


# --- _write_day_sum: реальная таблица, тестовая дата не пересекается с прод -

TEST_DATE = "2099-01-01"


@pytest.fixture(autouse=True)
def cleanup_day_sum():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.day_sum WHERE "Date" = %s', (TEST_DATE,))
        conn.commit()


def test_write_day_sum_maps_alcohol_column_name():
    d = {"User_ID": "тест", "Target_Date": TEST_DATE, **{f: 1 for f in nr.FIELDS}}
    with get_conn() as conn, conn.cursor() as cur:
        nr._write_day_sum(cur, d)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Алкоголь, гр", "Calories" FROM health.day_sum WHERE "Date" = %s', (TEST_DATE,))
        row = cur.fetchone()
    assert row == ("1", "1")


def test_write_day_sum_upserts_by_date():
    d = {"User_ID": "тест", "Target_Date": TEST_DATE, **{f: 1 for f in nr.FIELDS}}
    with get_conn() as conn, conn.cursor() as cur:
        nr._write_day_sum(cur, d)
        conn.commit()
    d2 = {**d, "Calories": 999}
    with get_conn() as conn, conn.cursor() as cur:
        nr._write_day_sum(cur, d2)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Calories" FROM health.day_sum WHERE "Date" = %s', (TEST_DATE,))
        assert cur.fetchone() == ("999",)
        cur.execute('SELECT count(*) FROM health.day_sum WHERE "Date" = %s', (TEST_DATE,))
        assert cur.fetchone() == (1,)


# --- run_daily / run_weekly (мокаем всё внешнее) ---------------------------

def test_run_daily_no_meals_sends_reminder_only(monkeypatch):
    monkeypatch.setattr(nr, "build_daily_report", lambda cur: None)
    calls = []
    monkeypatch.setattr(nr.telegram, "send_message", lambda *a, **kw: calls.append(a))
    nr.run_daily()
    assert len(calls) == 1
    assert "не забудь" in calls[0][1].lower()


def test_run_weekly_no_meals_sends_reminder_only(monkeypatch):
    monkeypatch.setattr(nr, "build_weekly_report", lambda cur: None)
    calls = []
    monkeypatch.setattr(nr.telegram, "send_message", lambda *a, **kw: calls.append(a))
    nr.run_weekly()
    assert len(calls) == 1


def test_call_model_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert nr.call_model("тест", max_tokens=100, reasoning_tokens=50, temperature=0.1) == ""
