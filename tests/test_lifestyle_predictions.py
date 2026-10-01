"""Вклад образа жизни за день, потенциал, журнал предсказаний (2026-10-01, фаза 6)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg import sql

from app import lifestyle, predictions, vita
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


# ─────── формулы (вынесены из dashboard, результат не должен измениться) ───────

def _by_key(eff):
    return {e["key"]: e for e in eff}


def test_effects_formulas_match_the_old_dashboard_numbers():
    eff = _by_key(lifestyle.effects(442, 15421, 10000, 0, {"consumed": 42.2, "cap": 38.0},
                                    {"pct": 75}, {"pct": 22}))
    assert eff["steps"]["est_years"] == pytest.approx(-0.0197, abs=1e-4) and eff["steps"]["weight"] == "strong"
    assert eff["sleep"]["est_years"] is None and eff["sleep"]["what"] == "сон в зоне 7–9 ч"
    assert eff["fiber"]["est_years"] < 0 and eff["alcohol"]["what"] == "без алкоголя вчера"
    assert eff["fatsugar"]["weight"] == "weak"


def test_short_sleep_adds_age_and_missing_inputs_are_skipped():
    eff = _by_key(lifestyle.effects(300, None, 10000, None, None, None, None))
    assert eff["sleep"]["est_years"] > 0 and "steps" not in eff and "alcohol" not in eff and "fiber" not in eff


def test_alcohol_adds_age():
    eff = _by_key(lifestyle.effects(None, None, 10000, 40, None, None, None))
    assert eff["alcohol"]["est_years"] > 0 and eff["alcohol"]["weight"] == "moderate"


# ─────── вклад закрытого дня ───────

def _rows():
    return {"2026-09-29": {"Дата": "2026-09-29", "Чистый_сон_мин": 450, "Шаги_за_вчера": "9000"},
            "2026-09-30": {"Дата": "2026-09-30", "Чистый_сон_мин": 300, "Шаги_за_вчера": "14000"}}


def test_lifestyle_for_day_uses_own_day_inputs_with_steps_from_next_row():
    # шаги дня 09-29 лежат в строке 09-30: 14000 → выше цели 10000 → моложе; сон 09-29 = 450 мин — в зоне
    r = vita.lifestyle_for_day("2026-09-29", _rows(), [], [], 10000, (420, 540))
    assert r["date"] == "2026-09-29" and r["total_days"] < 0
    f = {x["key"]: x for x in r["factors"]}
    assert f["steps"]["days"] < 0 and f["steps"]["source"] == "Han 2023" and f["steps"]["grade"] == "сильные данные"
    assert "Источник" not in f["steps"]["how"]


def test_lifestyle_for_day_short_sleep_costs_days_and_no_inputs_is_none():
    r = vita.lifestyle_for_day("2026-09-30", _rows(), [], [], 10000, (420, 540))      # сон 300 мин — короче нормы
    assert next(x for x in r["factors"] if x["key"] == "sleep")["days"] > 0
    assert vita.lifestyle_for_day("2026-01-01", {}, [], [], 10000, (420, 540)) is None


def test_lifestyle_for_day_alcohol_from_meals_trace_is_not_a_drink():
    meals = [{"Date": "2026-09-29T20:00", "Алкоголь": "0.5"}]
    f = {x["key"]: x for x in vita.lifestyle_for_day("2026-09-29", _rows(), meals, [], 10000, (420, 540))["factors"]}
    assert f["alcohol"]["days"] == 0
    meals = [{"Date": "2026-09-29T20:00", "Алкоголь": "30"}]
    f = {x["key"]: x for x in vita.lifestyle_for_day("2026-09-29", _rows(), meals, [], 10000, (420, 540))["factors"]}
    assert f["alcohol"]["days"] > 0


def test_split_source():
    assert vita._split_source("Объяснение. Источник: Jiao 2015, Jain 2025.") == ("Объяснение.", "Jiao 2015, Jain 2025")
    assert vita._split_source("Без источника") == ("Без источника", None)


# ─────── потенциал ───────

def test_potential_is_sum_of_age_adding_marker_contributions():
    bio = {"phenoage": {"value": 36.0, "chrono_age": 44.1, "delta": -8.1},
           "drivers": [{"label": "Глюкоза", "years": 0.85, "type": "pos"}, {"label": "RDW", "years": 0.72, "type": "pos"},
                       {"label": "Лейкоциты", "years": -1.04, "type": "neg"}, {"label": "Хроно", "years": 44.1, "type": "total"}],
           "biomarkers": []}
    me = vita.shape_me(bio)
    assert me["potential"] == {"years": -1.6, "markers": ["Глюкоза", "RDW"]}
    assert vita.shape_me({"phenoage": {}, "drivers": [], "biomarkers": []})["potential"] is None


# ─────── журнал предсказаний ───────

def _snapshot(cur, day, score):
    cur.execute(sql.SQL("INSERT INTO {t} (date, ring, chips) VALUES (%s, %s, '{{}}'::jsonb) ON CONFLICT (date) DO UPDATE SET ring = EXCLUDED.ring")
                .format(t=sql.Identifier(schema(), "vita_day_snapshot")), (day, f'{{"score": {score}}}'))


def test_record_prediction_is_idempotent_first_wins():
    with get_conn() as conn, conn.cursor() as cur:
        assert predictions.record_prediction(cur, "day_index", date(2026, 10, 2), 84) is True
        assert predictions.record_prediction(cur, "day_index", date(2026, 10, 2), 50) is False
        cur.execute(sql.SQL("SELECT predicted FROM {t} WHERE kind='day_index' AND target_date=%s")
                    .format(t=sql.Identifier(schema(), "prediction_log")), (date(2026, 10, 2),))
        assert float(cur.fetchone()[0]) == 84


def test_fill_observed_from_closed_day_snapshot_and_accuracy():
    with get_conn() as conn, conn.cursor() as cur:
        for i, (pred, obs) in enumerate([(88, 85), (80, 84), (90, 79)]):
            d = date(2026, 10, 10 + i)
            predictions.record_prediction(cur, "day_index", d, pred)
            _snapshot(cur, d, obs)
        predictions.record_prediction(cur, "day_index", date(2026, 10, 13), 70)   # день ещё не закрыт — снимка нет
        assert predictions.fill_observed(cur) == 3
        acc = predictions.accuracy(cur)
    assert acc["n"] == 3 and acc["mae"] == 6.0 and acc["bias"] == 3.3          # ошибки +3, −4, +11
    assert acc["enough"] is False and acc["alert"] is False                    # мало дней — честно «копится»


def test_accuracy_alert_needs_enough_days_and_big_error():
    with get_conn() as conn, conn.cursor() as cur:
        for i in range(15):
            d = date(2026, 11, 10) + timedelta(days=i)
            predictions.record_prediction(cur, "day_index", d, 90)
            _snapshot(cur, d, 70)                                              # всегда на 20 выше факта
        predictions.fill_observed(cur)
        acc = predictions.accuracy(cur)
    assert acc["n"] == 15 and acc["mae"] == 20.0 and acc["alert"] is True and acc["bias"] == 20.0


def test_accuracy_empty_is_not_alert():
    with get_conn() as conn, conn.cursor() as cur:
        acc = predictions.accuracy(cur, kind="nothing")
    assert acc == {"n": 0, "mae": None, "bias": None, "max_err": None, "enough": False, "alert": False}


def test_system_check_reports_inaccurate_forecast(monkeypatch):
    from app import system_check
    monkeypatch.setattr(predictions, "accuracy", lambda cur: {"n": 20, "mae": 14.2, "bias": 9.0, "alert": True, "enough": True, "max_err": 30})
    problems = []
    system_check._check_prediction_accuracy(None, problems)
    assert len(problems) == 1 and "14.2" in problems[0] and "Прогноз дня" in problems[0]
    monkeypatch.setattr(predictions, "accuracy", lambda cur: {"n": 5, "mae": 2.0, "bias": 0, "alert": False, "enough": False, "max_err": 3})
    problems = []
    system_check._check_prediction_accuracy(None, problems)
    assert problems == []
