"""«Мои цели» (2026-10-01): переопределения, рамки врача, потребители. Схема card_test (как у всех тестов)."""
import pytest
from psycopg import sql

from app import goals, steps_pace, vita
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


@pytest.fixture(autouse=True)
def _fresh_cache():
    goals.cache_clear()
    yield
    goals.cache_clear()


def _frame(cur, key, hi=None, lo=None, src="врач · тест"):
    cur.execute(sql.SQL("INSERT INTO {t} (key, source, frame_lo, frame_hi, frame_source) VALUES (%s, 'по умолчанию', %s, %s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET frame_lo = EXCLUDED.frame_lo, frame_hi = EXCLUDED.frame_hi, frame_source = EXCLUDED.frame_source")
                .format(t=sql.Identifier(schema(), "goal")), (key, lo, hi, src))
    goals.cache_clear()


def test_no_override_means_base_value_and_default_source():
    with get_conn() as conn, conn.cursor() as cur:
        by = {g["key"]: g for g in goals.list_goals(cur)}
    assert by["steps_daily"]["value"] == 10000 and by["steps_daily"]["mine"] is False
    assert by["steps_daily"]["source"] == "по умолчанию"
    assert goals.get("steps_daily") == 10000 and goals.steps_target() == 10000


def test_set_goal_overrides_everywhere_and_marks_source_me():
    with get_conn() as conn, conn.cursor() as cur:
        goals.set_goal(cur, "steps_daily", 12000)
        by = {g["key"]: g for g in goals.list_goals(cur)}
    assert by["steps_daily"]["value"] == 12000 and by["steps_daily"]["source"] == "я" and by["steps_daily"]["mine"]
    assert goals.steps_target() == 12000
    assert steps_pace.expected_steps(22.0) == 12000                      # темп дня берёт новую цель
    assert steps_pace.pace(6000, 14.5)["score"] == 100                   # ожидалось 6000
    assert steps_pace.closed_day_score(6000) == 50


def test_doctor_frame_blocks_values_outside_but_allows_stricter_inside():
    with get_conn() as conn, conn.cursor() as cur:
        _frame(cur, "sat_fat_g", hi=28)
        with pytest.raises(goals.GoalError, match="Рамка врача"):
            goals.set_goal(cur, "sat_fat_g", 35)
        assert goals.set_goal(cur, "sat_fat_g", 24)["value"] == 24       # строже рамки — можно
        by = {g["key"]: g for g in goals.list_goals(cur)}
    assert by["sat_fat_g"]["frame"]["hi"] == 28 and by["sat_fat_g"]["value"] == 24


def test_doctor_lower_frame_blocks_lowering():
    with get_conn() as conn, conn.cursor() as cur:
        _frame(cur, "swim_per_week", lo=2)
        with pytest.raises(goals.GoalError):
            goals.set_goal(cur, "swim_per_week", 1)
        assert goals.set_goal(cur, "swim_per_week", 3)["value"] == 3


def test_sane_bounds_unknown_key_and_garbage_input():
    with get_conn() as conn, conn.cursor() as cur:
        with pytest.raises(goals.GoalError, match="допустимо"):
            goals.set_goal(cur, "steps_daily", 50)
        with pytest.raises(goals.GoalError):
            goals.set_goal(cur, "nope", 5)
        with pytest.raises(goals.GoalError, match="число"):
            goals.set_goal(cur, "steps_daily", "много")


def test_reset_returns_to_base_and_keeps_doctor_frame():
    with get_conn() as conn, conn.cursor() as cur:
        _frame(cur, "sat_fat_g", hi=28)
        goals.set_goal(cur, "sat_fat_g", 20)
        goals.reset_goal(cur, "sat_fat_g")
        by = {g["key"]: g for g in goals.list_goals(cur)}
    assert by["sat_fat_g"]["mine"] is False and by["sat_fat_g"]["frame"]["hi"] == 28


def test_apply_limits_and_profile_use_overrides_only_for_goal_nutrients():
    with get_conn() as conn, conn.cursor() as cur:
        goals.set_goal(cur, "sodium_mg", 2300)
        goals.set_goal(cur, "protein_g", 140)
    targets = [{"Нутриент": "Натрий", "Верхний_предел_UL": "2800"}, {"Нутриент": "Кальций", "Верхний_предел_UL": "2500"}]
    out = goals.apply_limits(targets)
    assert out[0]["Верхний_предел_UL"] == "2300" and out[1]["Верхний_предел_UL"] == "2500"
    assert targets[0]["Верхний_предел_UL"] == "2800"                      # исходные строки не мутируются
    prof = goals.apply_profile({"Protein_target": "160", "Calories_target": "2400"})
    assert prof["Protein_target"] == "140" and prof["Calories_target"] == "2400"


def test_habit_goals_feed_rx_needs_and_sleep_zone():
    with get_conn() as conn, conn.cursor() as cur:
        goals.set_goal(cur, "swim_per_week", 3)
        goals.set_goal(cur, "sleep_min_h", 7.5)
    assert vita._rx_need("swim", 2) == 3 and vita._rx_need("walk", 7) == 7
    assert vita._rx_goal_text("swim", 10000) == "3 раза в неделю"
    assert goals.sleep_zone_min() == (450, 540)


def test_goals_read_failure_falls_back_to_defaults(monkeypatch):
    goals.cache_clear()
    monkeypatch.setattr(goals, "_load_rows", lambda cur: (_ for _ in ()).throw(RuntimeError("db down")))
    assert goals.get("steps_daily") == 10000 and goals.steps_target() == 10000


# ─────── эндпоинты ───────

def _client_cookie():
    from fastapi.testclient import TestClient
    from app import vita_auth as va
    from app.main import app
    return TestClient(app), {va.COOKIE_NAME: va.create_session_token()}


def test_goals_endpoints_require_session():
    c, _ = _client_cookie()
    assert c.get("/vita/goals").status_code == 401
    assert c.post("/vita/goals", json={"key": "steps_daily", "value": 12000}).status_code == 401
    assert c.post("/vita/goals/reset", json={"key": "steps_daily"}).status_code == 401


def test_goals_endpoint_roundtrip_set_then_reset():
    c, ck = _client_cookie()
    r = c.post("/vita/goals", cookies=ck, json={"key": "steps_daily", "value": 12000})
    assert r.status_code == 200 and r.json()["value"] == 12000
    g = {x["key"]: x for x in c.get("/vita/goals", cookies=ck).json()["goals"]}
    assert g["steps_daily"]["value"] == 12000 and g["steps_daily"]["source"] == "я"
    assert c.post("/vita/goals/reset", cookies=ck, json={"key": "steps_daily"}).status_code == 200
    g = {x["key"]: x for x in c.get("/vita/goals", cookies=ck).json()["goals"]}
    assert g["steps_daily"]["value"] == 10000 and g["steps_daily"]["mine"] is False


def test_goals_endpoint_returns_400_with_readable_message_for_doctor_frame():
    c, ck = _client_cookie()
    with get_conn() as conn, conn.cursor() as cur:
        _frame(cur, "sat_fat_g", hi=28, src="консилиум 30 сент.")
    r = c.post("/vita/goals", cookies=ck, json={"key": "sat_fat_g", "value": 40})
    assert r.status_code == 400 and "консилиум 30 сент." in r.json()["detail"] and "28" in r.json()["detail"]
    assert c.post("/vita/goals", cookies=ck, json={"key": "bogus", "value": 1}).status_code == 400
