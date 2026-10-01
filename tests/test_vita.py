"""Vita v1 (2026-09-26) — app/vita.py. Юниты на чистые функции (скор, состояние,
чипы, нудж) + FakeCursor для _dish_sources/build_levers (health.* без тестовой
схемы, тот же принцип, что test_doctor_context.py) + TestClient для 401/200 на
эндпоинтах. Часть 3 тикета: все 4 обязательных состояния — API-уровнем."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import vita
from app import vita_auth as va
from app.main import app

client = TestClient(app)


# ─────── _score_from_judgments / _budget_judgment — веса зафиксированы в docstring ───────

def test_score_empty_judgments_is_none_not_zero():
    """День не оценивается — не «оценка 0» (тот самый урок null≠0)."""
    assert vita._score_from_judgments([]) is None


def test_score_all_good_is_100():
    assert vita._score_from_judgments(["good", "good", "good"]) == 100


def test_score_penalty_escalates_not_linear():
    """Смысл BAD_STEP=[14,18,20,...]: штраф ЭСКАЛИРУЕТ — второй провал стоит
    дороже первого (не «каждый провал −14 линейно»), один сбой почти не
    трогает скор, но сыплющийся день обрушивается быстро."""
    penalty_1 = 100 - vita._score_from_judgments(["bad"])
    penalty_2 = 100 - vita._score_from_judgments(["bad", "bad"])
    marginal_cost_of_second = penalty_2 - penalty_1
    assert marginal_cost_of_second > penalty_1  # 18 > 14: второй провал дороже первого


def test_score_never_below_zero():
    assert vita._score_from_judgments(["bad"] * 20) == 0


def test_score_matches_documented_step_arrays():
    # 2 bad + 1 warn = 100 - (14+18) - 4 = 64, по BAD_STEP/WARN_STEP в докстринге
    assert vita._score_from_judgments(["bad", "bad", "warn"]) == 64


def test_budget_judgment_clinical_over_is_bad():
    assert vita._budget_judgment({"kind": "limit", "status": "over", "label": "Натрий"}) == "bad"


def test_budget_judgment_nonclinical_over_is_warn():
    assert vita._budget_judgment({"kind": "limit", "status": "over", "label": "Кофеин"}) == "warn"


def test_budget_judgment_close_is_warn():
    assert vita._budget_judgment({"kind": "limit", "status": "close", "label": "Натрий"}) == "warn"


def test_budget_judgment_ok_is_good():
    assert vita._budget_judgment({"kind": "limit", "status": "ok", "label": "Натрий"}) == "good"


def test_budget_judgment_goal_kind_is_always_good():
    assert vita._budget_judgment({"kind": "goal", "status": "over", "label": "Клетчатка"}) == "good"


# ─────── _collect_judgments — 4 сегмента (Vita v2, этап 1) ───────

def _decision_reasons(bb_judgment=None, hrv_judgment=None, move_judgment=None):
    reasons = []
    if bb_judgment:
        reasons.append({"label": "Body Battery", "value": 80, "judgment": bb_judgment, "segment": "recovery"})
    if hrv_judgment:
        reasons.append({"label": "ВСР к базе", "value": "+2 мс", "judgment": hrv_judgment, "segment": "recovery"})
    if move_judgment:
        reasons.append({"label": "ACWR (Garmin)", "value": "1.2", "judgment": move_judgment, "segment": "move"})
    return reasons


def test_collect_judgments_recovery_segment_from_reasons():
    today = {"decision": {"reasons": _decision_reasons(bb_judgment="bad", hrv_judgment="good")}, "budget": []}
    crit = vita._collect_judgments(today, {"metrics": []})
    assert crit["recovery"] == ["bad", "good"]


def test_collect_judgments_move_segment_combines_steps_and_acwr_reason():
    today = {"decision": {"reasons": _decision_reasons(move_judgment="bad")}, "budget": []}
    health = {"metrics": [{"key": "steps", "value": 4200, "judgment": "warn"}]}
    crit = vita._collect_judgments(today, health)
    assert sorted(crit["move"]) == ["bad", "warn"]


def test_collect_judgments_ignores_judgment_without_a_value():
    """Живой баг (2026-09-28): _baseline_metrics_for_index кладёт judgment=
    'neutral' по умолчанию ДАЖЕ когда метрики за день реально нет (value=None) —
    раньше это было незаметно (neutral не штрафует в любом случае), но с
    _day_index (среднее сегментов) превращалось в фантомную "сотню" для
    сегмента без единого реального числа."""
    health = {"metrics": [{"key": "steps", "value": None, "judgment": "neutral"}]}
    crit = vita._collect_judgments({"decision": {"reasons": []}, "budget": []}, health)
    assert crit["move"] == []


def test_collect_judgments_food_segment_is_budget_limits_only():
    today = {"decision": {"reasons": []}, "budget": [
        {"kind": "limit", "status": "over", "label": "Натрий"},
        {"kind": "goal", "status": "over", "label": "Клетчатка"},
    ]}
    crit = vita._collect_judgments(today, {"metrics": []})
    assert crit["food"] == ["bad"]  # Натрий клинический -> bad; Клетчатка -- goal, не считается


def test_scores_includes_recovery_score():
    today = {"decision": {"reasons": _decision_reasons(bb_judgment="good", hrv_judgment="good")}, "budget": []}
    scores = vita._scores(today, {"metrics": []})
    assert scores["recovery_score"] == 100


# ─────── build_index_breakdown — «из чего индекс» (Vita v2, этап 1) ───────

def test_build_index_breakdown_includes_reasons_and_labels():
    today = {"decision": {"reasons": _decision_reasons(bb_judgment="bad")}, "budget": []}
    rows = vita.build_index_breakdown(today, {"metrics": []})
    assert rows == [{"mark": "n", "label": "Body Battery", "detail": "80", "segment": "recovery"}]


def test_build_index_breakdown_includes_sleep_and_stress_metrics():
    health = {"metrics": [{"key": "sleep_min", "value": 424, "unit": "мин", "judgment": "good"},
                           {"key": "stress", "value": 45, "unit": "", "judgment": "warn"}]}
    rows = vita.build_index_breakdown({"decision": {"reasons": []}, "budget": []}, health)
    labels = {r["label"]: r for r in rows}
    assert labels["Сон"]["mark"] == "y" and labels["Сон"]["detail"] == "424 мин"
    assert labels["Сон"]["segment"] == "sleep"
    assert labels["Стресс"]["mark"] == "w"
    assert labels["Стресс"]["segment"] == "recovery"


def test_build_index_breakdown_includes_budget_limits_only():
    today = {"decision": {"reasons": []}, "budget": [
        {"kind": "limit", "status": "over", "label": "Натрий", "consumed": 6.2, "cap": 5, "unit": "г"},
        {"kind": "goal", "status": "over", "label": "Клетчатка", "consumed": 30, "cap": 25, "unit": "г"},
    ]}
    rows = vita.build_index_breakdown(today, {"metrics": []})
    assert rows == [{"mark": "n", "label": "Натрий", "detail": "6.2/5 г", "segment": "food"}]


def test_build_index_breakdown_empty_when_no_data():
    assert vita.build_index_breakdown({"decision": {"reasons": []}, "budget": []}, {"metrics": []}) == []


# ─────── детали кругляша (Vita v2, этап 2 — живая просьба Влада, реальные шторки макета) ───────

def _fake_hrv_history(values):
    return [{"date": f"2026-09-{i+1:02d}", "ВСР_ночная": v} for i, v in enumerate(values)]


def test_build_recovery_detail_computes_hrv_averages(monkeypatch):
    monkeypatch.setattr(vita, "_recent_daily_values", lambda cur, cols, days:
                         _fake_hrv_history([40, 41, 42, 43, 44, 45, 46]) if cols == ["ВСР_ночная"]
                         else [{"date": "2026-09-28", "Пульс_ночной_средний": 54}])
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [])
    today = {"decision": {"acwr": 0.9, "acwr_status": "LOW", "load_high": False}}
    out = vita.build_recovery_detail(_FC(), today, {"blocked": False})
    assert out["hrv_last"] == 46
    assert out["hrv_avg7"] == 43.0
    assert out["hrv_avg30"] == 43.0
    assert out["resting_hr"] == 54
    assert "Нагрузка низкая" in out["coach"]


def test_build_recovery_detail_load_high_overrides_coach(monkeypatch):
    monkeypatch.setattr(vita, "_recent_daily_values", lambda cur, cols, days: [])
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [])
    out = vita.build_recovery_detail(_FC(), {"decision": {"acwr": 2.0, "acwr_status": "HIGH", "load_high": True}},
                                      {"blocked": True, "label": "щадящий режим · L5/S1"})
    assert "Нагрузка выше обычного" in out["coach"]
    assert "щадящий режим" in out["coach"]
    assert out["hrv_last"] is None and out["hrv_avg7"] is None


def test_build_sleep_detail_flags_shorter_than_average(monkeypatch):
    # Проверено на реальных данных (2026-09-28): Легкий+Глубокий+REM = Чистый_сон_мин.
    history = [{"date": f"2026-09-{i+1:02d}", "Чистый_сон_мин": 420, "Легкий_сон_мин": 220,
                "Глубокий_сон_мин": 110, "REM_сон_мин": 90, "Бодрствование_мин": 8} for i in range(14)]
    monkeypatch.setattr(vita, "_recent_daily_values", lambda cur, cols, days: history)
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [])
    out = vita.build_sleep_detail(_FC(), sleep_min_today=380, sleep_quality_today=79.0)
    assert out["avg14_min"] == 420
    assert out["delta_min"] == -40
    assert "Короче среднего" in out["coach"]
    assert out["sleep_score"] == 79.0
    assert out["light_min"] == 220 and out["deep_min"] == 110 and out["rem_min"] == 90 and out["awake_min"] == 8
    assert len(out["history"]) == 14 and out["history"][0]["hours"] == 7.0


def test_build_sleep_detail_no_history_is_honest_not_fake():
    out = vita.build_sleep_detail(_FC(), sleep_min_today=400, sleep_quality_today=None)
    assert out["avg14_min"] is None and out["delta_min"] is None
    assert "не хватает" in out["coach"]
    assert out["sleep_score"] is None


def test_build_move_detail_uses_gate_for_coach(monkeypatch):
    monkeypatch.setattr(vita, "_recent_daily_values", lambda cur, cols, days: [])
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [])
    steps = {"now_steps": 4200, "target": 12000, "status_word": "в темпе", "behind_pace": False}
    today = {"decision": {"acwr": 0.9, "acwr_status": "LOW"}}
    out = vita.build_move_detail(_FC(), today, steps, {"blocked": True, "label": "щадящий режим"})
    assert out["steps_now"] == 4200 and out["steps_target"] == 12000
    assert out["acwr"] == 0.9 and out["acwr_status"] == "LOW"
    assert out["workouts"] == []  # живая жалоба: раньше тренировки вообще не показывались нигде
    assert "щадящего режима" in out["coach"]


def test_todays_workouts_skips_empty_slots(monkeypatch):
    class _WorkoutCur(_FC):
        def fetchone(self):
            return ("Бег", "32", "Нет", "", None, "0")
    out = vita._todays_workouts(_WorkoutCur())
    assert out == [{"type": "Бег", "minutes": 32}]


def test_build_food_topic_detail_returns_publications_and_heatmap(monkeypatch):
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [{"title": "x", "grade": "RCT", "why": "", "url": None}])
    monkeypatch.setattr("app.dashboard.get_weekly_nutrition", lambda cur: {
        "heatmap": [{"label": "Магний", "avgPct": 62, "level": 2, "unit": "мг", "values": [60, 55, 70, 58, 65, 61, 63]}],
    })
    out = vita.build_food_topic_detail(_FC())
    assert out["segment"] == "food"
    assert len(out["publications"]) == 1
    assert out["micro_heatmap"] == [{"label": "Магний", "avg_pct": 62, "level": 2, "unit": "мг",
                                      "days_pct": [60, 55, 70, 58, 65, 61, 63],
                                      # сверка с макетом v7: справка и «откуда был за неделю» для тапа по строке
                                      "note": None, "top_sources": [], "upper_pct": None}]
    assert out["yesterday"] == {"meals": [], "protein": 0, "kcal": 0, "sat_fat": None}


def test_build_food_topic_detail_empty_heatmap_when_nothing_deviates(monkeypatch):
    monkeypatch.setattr(vita, "_topic_publications", lambda cur, seg, limit=5: [])
    monkeypatch.setattr("app.dashboard.get_weekly_nutrition", lambda cur: {"heatmap": []})
    out = vita.build_food_topic_detail(_FC())
    assert out["micro_heatmap"] == []


# ─────── ring.ahead — виртуальное выполнение главной подсказки ───────

def test_main_action_segment_food_when_protein_gap_large():
    state = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    steps = {"behind_pace": False}
    protein = {"target": 160, "consumed": 100}
    assert vita._main_action_segment(state, steps, protein) == "food"


def test_main_action_segment_move_when_behind_pace_and_protein_ok():
    state = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    steps = {"behind_pace": True}
    protein = {"target": 160, "consumed": 155}
    assert vita._main_action_segment(state, steps, protein) == "move"


def test_main_action_segment_none_when_no_watch_or_no_food():
    steps, protein = {"behind_pace": True}, {"target": 160, "consumed": 50}
    assert vita._main_action_segment({"no_watch": True, "no_food": False, "time_of_day": "day"}, steps, protein) is None
    assert vita._main_action_segment({"no_watch": False, "no_food": True, "time_of_day": "day"}, steps, protein) is None


def test_main_action_segment_none_when_evening():
    state = {"no_watch": False, "no_food": False, "time_of_day": "evening"}
    assert vita._main_action_segment(state, {"behind_pace": True}, {"target": None, "consumed": None}) is None


def test_score_with_segment_fixed_flips_worst_judgment_to_good():
    crit = {"overall": ["bad", "good", "warn"], "food": ["bad"], "move": ["warn"]}
    fixed = vita._score_with_segment_fixed(crit, "food")
    # overall становится ["good","good","warn"] -> только warn-штраф
    assert fixed == vita._score_from_judgments(["good", "good", "warn"])


# ─────── _day_index / _segment_score_with_fixed (живая правка 2026-09-28: ───────
# ─────── «кругляши поменяли, итоговый индекс дня нет, он от них зависит») ───────

def test_day_index_uses_real_garmin_numbers_when_available():
    scores = {"recovery_score": 100, "sleep_score": 100, "movement_score": 100, "nutrition_score": 100}
    chips = {"energy": 68, "sleep_quality": 86}
    # (68 + 86 + 100 + 100) / 4 = 88.5 -> round -> 88 (банковское округление .5 к чётному)
    assert vita._day_index(scores, chips) == 88


def test_day_index_falls_back_to_judgment_score_without_garmin_numbers():
    scores = {"recovery_score": 80, "sleep_score": 90, "movement_score": 70, "nutrition_score": 60}
    assert vita._day_index(scores, {}) == 75  # (80+90+70+60)/4


def test_day_index_none_when_all_segments_missing():
    assert vita._day_index({"recovery_score": None, "sleep_score": None,
                             "movement_score": None, "nutrition_score": None}, {}) is None


def test_day_index_averages_only_available_segments():
    scores = {"recovery_score": None, "sleep_score": None, "movement_score": 60, "nutrition_score": 80}
    assert vita._day_index(scores, {}) == 70


def test_segment_score_with_fixed_flips_only_that_segments_worst_judgment():
    crit = {"food": ["bad", "good"], "move": ["warn"]}
    assert vita._segment_score_with_fixed(crit, "food") == vita._score_from_judgments(["good", "good"])
    assert vita._segment_score_with_fixed(crit, "move") == vita._score_from_judgments(["good"])


def test_segment_score_with_fixed_none_when_segment_has_no_judgments():
    assert vita._segment_score_with_fixed({"food": []}, "food") is None


def test_compute_ahead_equals_current_when_no_actionable_segment():
    today = {"decision": {"reasons": _decision_reasons(bb_judgment="good")}, "budget": []}
    state = {"no_watch": False, "no_food": False, "time_of_day": "evening"}
    ahead = vita.compute_ahead(today, {"metrics": []}, state, {"behind_pace": False},
                                {"target": None, "consumed": None}, chips={})
    assert ahead == 100


def test_compute_ahead_is_higher_than_current_when_food_fixable():
    """Живая правка (2026-09-28, «индекс дня зависит от кругляшей») —
    compute_ahead теперь считает через _day_index (среднее сегментов), не
    через escalation по объединённому overall-списку; current здесь
    посчитан тем же способом, каким его считает сам compute_ahead."""
    today = {"decision": {"reasons": []}, "budget": [{"kind": "limit", "status": "over", "label": "Натрий"}]}
    state = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    protein = {"target": 160, "consumed": 100}
    crit = vita._collect_judgments(today, {"metrics": []})
    scores = {"recovery_score": vita._score_from_judgments(crit["recovery"]),
              "sleep_score": vita._score_from_judgments(crit["sleep"]),
              "movement_score": vita._score_from_judgments(crit["move"]),
              "nutrition_score": vita._score_from_judgments(crit["food"])}
    current = vita._day_index(scores, {})
    ahead = vita.compute_ahead(today, {"metrics": []}, state, {"behind_pace": False}, protein, chips={})
    assert ahead > current


# ─────── CHIP_NORM / chip_status (Vita v2, этап 1) ───────

def test_chip_status_good_when_at_or_above_norm():
    scores = {"recovery_score": 65, "sleep_score": 100, "movement_score": 70, "nutrition_score": 70}
    status = vita.chip_status(scores, vita.DEFAULT_CHIP_NORM)
    assert status == {"recovery": "good", "sleep": "good", "move": "good", "food": "good"}


def test_chip_status_warn_below_norm():
    scores = {"recovery_score": 64, "sleep_score": 69, "movement_score": 69, "nutrition_score": 69}
    status = vita.chip_status(scores, vita.DEFAULT_CHIP_NORM)
    assert all(v == "warn" for v in status.values())


def test_chip_status_none_when_segment_not_scored():
    scores = {"recovery_score": None, "sleep_score": 80, "movement_score": None, "nutrition_score": 80}
    status = vita.chip_status(scores, vita.DEFAULT_CHIP_NORM)
    assert status["recovery"] is None and status["move"] is None


def test_chip_status_respects_custom_norm_from_profile():
    scores = {"recovery_score": 80, "sleep_score": 80, "movement_score": 80, "nutrition_score": 80}
    custom = {"recovery": 90, "sleep": 70, "move": 70, "food": 70}  # только recovery строже
    status = vita.chip_status(scores, custom)
    assert status["recovery"] == "warn" and status["sleep"] == "good"


def test_chip_status_uses_real_garmin_numbers_for_sleep_and_recovery():
    """Живая жалоба Влада (2026-09-28): «сон 7:16 — это 100, а 8:00 будет
    120?» — кругляши «Сон»/«Заряд» красятся судейским скором (100 при любом
    "не плохо"), а не настоящим числом Гармина. sleep_score/recovery_score
    в судействе тут вообще 100 (никакого bad/warn), но реальный Body
    Battery=60 и Оценка сна=65 — НИЖЕ порога, статус должен быть warn."""
    scores = {"recovery_score": 100, "sleep_score": 100, "movement_score": 100, "nutrition_score": 100}
    chips = {"energy": 60, "sleep_quality": 65}  # оба ниже DEFAULT_CHIP_NORM (65/70)
    status = vita.chip_status(scores, vita.DEFAULT_CHIP_NORM, chips)
    assert status["recovery"] == "warn"  # 60 < 65
    assert status["sleep"] == "warn"     # 65 < 70
    assert status["move"] == "good" and status["food"] == "good"  # для них судейский скор остаётся


def test_chip_status_falls_back_to_judgment_score_when_no_garmin_number():
    """Часы не дали Body Battery/Оценку сна сегодня — не падаем на None,
    используем прежний судейский скор (честная деградация, не «нет данных»
    там, где judgment-формула вообще-то что-то знает)."""
    scores = {"recovery_score": 80, "sleep_score": 80, "movement_score": 80, "nutrition_score": 80}
    status = vita.chip_status(scores, vita.DEFAULT_CHIP_NORM, {"energy": None, "sleep_quality": None})
    assert status["recovery"] == "good" and status["sleep"] == "good"


# ─────── build_streaks — вехи/рекорд/atRisk на истории (Vita v2, этап 1) ───────

def _trend_row(day, sleep_min=450):
    return {"Дата": day, "Чистый_сон_мин": sleep_min}


_SODIUM_TARGET = [{"Нутриент": "Натрий", "Колонка_в_Meals": "Натрий", "Категория": "Риск избытка",
                   "Верхний_предел_UL": "2300", "Единица": "мг"}]


def test_build_streaks_counts_consecutive_days_and_record():
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]  # 10 дней подряд в норме сна
    rows = [_trend_row(d) for d in days]
    result = vita.build_streaks(rows, [], [], days[-1])
    sleep_streak = next(s for s in result["streaks"] if s["key"] == "sleep_zone")
    assert sleep_streak["count"] == 10
    assert sleep_streak["record"] == 10
    assert sleep_streak["next_milestone"] == 14


def test_build_streaks_breaks_on_real_gap_without_freeze():
    """Копилка (STREAK_FREEZE_POOL=2) — общая на все серии: если её уже
    израсходовали два БОЛЕЕ СВЕЖИХ провала натрия, провал сна 05.09 останется
    настоящим и порвёт серию, несмотря на существование копилки вообще."""
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]
    rows = [_trend_row(d, sleep_min=(300 if d == "2026-09-05" else 450)) for d in days]
    # два самых свежих провала (по НАТРИЮ, 09 и 10 сентября) съедают копилку
    # раньше, чем очередь дойдёт до более старого провала сна (05.09)
    recent_gap_days = {days[-1], days[-2]}
    meals = [{"Date": f"{d}T08:00", "Натрий": ("3000" if d in recent_gap_days else "500")} for d in days]
    result = vita.build_streaks(rows, meals, _SODIUM_TARGET, days[-1])
    sleep_streak = next(s for s in result["streaks"] if s["key"] == "sleep_zone")
    assert sleep_streak["count"] == 5  # только с 06 по 10 (после провала 05.09, копилка уже занята)
    assert sleep_streak["record"] == 5  # 06-10 (5 дней) длиннее, чем 01-04 (4 дня) до провала
    assert result["freezes_available"] == 0


def test_build_streaks_freeze_pool_covers_one_recent_gap():
    """Копилка (STREAK_FREEZE_POOL=2) отдаёт заморозку самому свежему провалу —
    серия не рвётся, freezes_available уменьшается."""
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]
    gap_day = days[-2]  # предпоследний день — провал у самого края (частый сценарий "почти сегодня")
    rows = [_trend_row(d, sleep_min=(300 if d == gap_day else 450)) for d in days]
    result = vita.build_streaks(rows, [], [], days[-1])
    sleep_streak = next(s for s in result["streaks"] if s["key"] == "sleep_zone")
    assert sleep_streak["count"] == 10  # провал заморожен — серия НЕ прервалась
    assert result["freezes_available"] == vita.STREAK_FREEZE_POOL - 1


def test_build_streaks_missing_data_day_neither_breaks_nor_extends():
    days = [f"2026-09-{d:02d}" for d in range(1, 6)]
    rows = [_trend_row(d) for d in days if d != "2026-09-03"]  # 03.09 — строки вообще нет (нет данных)
    result = vita.build_streaks(rows, [], [], days[-1])
    sleep_streak = next(s for s in result["streaks"] if s["key"] == "sleep_zone")
    assert sleep_streak["count"] == 4  # 01,02,04,05 — пропуск дня без данных не считается провалом


def test_build_streaks_nutrient_limit_streak_from_meals():
    days = [f"2026-09-{d:02d}" for d in range(1, 6)]
    rows = [_trend_row(d) for d in days]
    meals = [{"Date": f"{d}T08:00", "Натрий": "500"} for d in days]
    result = vita.build_streaks(rows, meals, _SODIUM_TARGET, days[-1])
    sodium_streak = next(s for s in result["streaks"] if s["key"] == "Натрий")
    assert sodium_streak["count"] == 5


def test_build_streaks_at_risk_when_close_to_limit():
    days = [f"2026-09-{d:02d}" for d in range(1, 4)]
    rows = [_trend_row(d) for d in days]
    meals = [{"Date": f"{d}T08:00", "Натрий": ("2000" if d == days[-1] else "500")} for d in days]  # 2000/2300 = 87%, "close"
    result = vita.build_streaks(rows, meals, _SODIUM_TARGET, days[-1])
    sodium_streak = next(s for s in result["streaks"] if s["key"] == "Натрий")
    assert sodium_streak["at_risk"] is True
    assert sodium_streak["status"] == "at_risk"


def test_build_streaks_no_history_returns_empty_list():
    result = vita.build_streaks([], [], [], "2026-09-28")
    assert result["streaks"] == []
    assert result["freezes_available"] == vita.STREAK_FREEZE_POOL


# ─────── write_day_snapshot / read_day_snapshot (Vita v2, этап 1, п.4) ───────
# card.vita_day_snapshot — обычная card.* таблица, изолирована схемой
# card_test (schema()) — отдельная защита не нужна, та же ситуация, что
# card.lab_request в тикете «оптимизатор сдачи анализов».

from datetime import date
from app.db import get_conn, schema


def test_write_day_snapshot_false_when_no_history_for_date():
    with get_conn() as conn, conn.cursor() as cur:
        written = vita.write_day_snapshot(cur, date(1999, 1, 1))
    assert written is False


def test_write_and_read_day_snapshot_roundtrip(monkeypatch):
    fake_rows_tuple = [(date(2026, 9, 20), {"Восстановление_BodyBattery": 80.0, "ВСР_ночная": 50.0,
                                             "Чистый_сон_мин": 450.0, "ACWR_Garmin": None, "ACWR_Status": None})]
    fake_rows_flat = [{"Дата": "2026-09-20", "Восстановление_BodyBattery": "80", "ВСР_ночная": "50",
                        "Чистый_сон_мин": "450", "ACWR_Garmin": "", "ACWR_Status": ""}]
    monkeypatch.setattr(vita, "_fetch_history", lambda cur: (fake_rows_tuple, fake_rows_flat, [], []))
    monkeypatch.setattr(vita, "read_chip_norm", lambda cur: dict(vita.DEFAULT_CHIP_NORM))
    with get_conn() as conn, conn.cursor() as cur:
        written = vita.write_day_snapshot(cur, date(2026, 9, 20))
        conn.commit()
        snap = vita.read_day_snapshot(cur, date(2026, 9, 20))
    assert written is True
    assert snap["date"] == "2026-09-20"
    # Живая правка (2026-09-28): индекс — среднее сегментов, «Заряд» берёт
    # реальный Body Battery=80 (не 100), Движение/Питание без данных не
    # считаются вовсе -> (80 + 100[сон, судейский]) / 2 = 90.
    assert snap["ring"]["score"] == 90
    assert snap["chips"]["sleep_min"] == 450.0
    assert snap["chips"]["energy"] == 80.0
    assert "chip_status" in snap["ring"]


def test_read_day_snapshot_none_when_absent():
    with get_conn() as conn, conn.cursor() as cur:
        assert vita.read_day_snapshot(cur, date(2000, 1, 1)) is None


def test_write_day_snapshot_upserts_on_conflict(monkeypatch):
    fake_rows_tuple = [(date(2026, 9, 21), {"Восстановление_BodyBattery": 40.0})]
    fake_rows_flat = [{"Дата": "2026-09-21", "Восстановление_BodyBattery": "40"}]
    monkeypatch.setattr(vita, "_fetch_history", lambda cur: (fake_rows_tuple, fake_rows_flat, [], []))
    monkeypatch.setattr(vita, "read_chip_norm", lambda cur: dict(vita.DEFAULT_CHIP_NORM))
    with get_conn() as conn, conn.cursor() as cur:
        vita.write_day_snapshot(cur, date(2026, 9, 21))
        vita.write_day_snapshot(cur, date(2026, 9, 21))  # повторная запись — не дублирует строку
        conn.commit()
        cur.execute(f"SELECT count(*) FROM {vita.schema()}.vita_day_snapshot WHERE date = '2026-09-21'")
        assert cur.fetchone()[0] == 1


# ─────── manual marks (Vita v2, этап 1, п.6) ───────

def test_write_manual_mark_rejects_unknown_field():
    with get_conn() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):
            vita.write_manual_mark(cur, date(2026, 9, 20), "not_a_real_field", True)


def test_write_and_read_manual_mark_roundtrip():
    with get_conn() as conn, conn.cursor() as cur:
        vita.write_manual_mark(cur, date(2026, 9, 22), "swim_happened", True)
        conn.commit()
        marks = vita._read_manual_marks(cur, "2026-09-22")
    assert marks == {"swim_happened": True}


def test_build_assignments_garmin_source_when_watch_saw_the_day():
    result = vita.build_assignments(cur=None, today={}, state={"no_watch": False})
    assert result["source"] == "garmin"


def test_build_assignments_manual_source_when_marks_exist():
    with get_conn() as conn, conn.cursor() as cur:
        vita.write_manual_mark(cur, date(2026, 9, 23), "movement_ok", True)
        conn.commit()
        result = vita.build_assignments(cur, {"date": "2026-09-23"}, {"no_watch": True})
    assert result["source"] == "manual"
    assert result["movement_ok"] is True


def test_build_assignments_none_source_when_no_watch_and_no_marks():
    with get_conn() as conn, conn.cursor() as cur:
        result = vita.build_assignments(cur, {"date": "2026-09-24"}, {"no_watch": True})
    assert result["source"] is None


# ─────── _time_of_day ───────

def test_time_of_day_morning():
    assert vita._time_of_day(datetime(2026, 9, 26, 8, 0)) == "morning"


def test_time_of_day_day():
    assert vita._time_of_day(datetime(2026, 9, 26, 16, 2)) == "day"


def test_time_of_day_evening():
    assert vita._time_of_day(datetime(2026, 9, 26, 21, 30)) == "evening"


def test_time_of_day_1936_is_still_day_not_evening():
    """Живой баг (2026-09-28): день закрывался уже в 19:36 — «день закрыт на
    96, а если я ещё что-то съем?» Порог сдвинут на 21 (из примера в
    докстринге), 19:36 должно остаться днём."""
    assert vita._time_of_day(datetime(2026, 9, 28, 19, 36)) == "day"


def test_time_of_day_boundary_21_is_evening():
    assert vita._time_of_day(datetime(2026, 9, 26, 21, 0)) == "evening"


def test_time_of_day_boundary_11_is_day_not_morning():
    assert vita._time_of_day(datetime(2026, 9, 26, 11, 0)) == "day"


# ─────── build_state — 4 обязательных состояния (Часть 3 тикета) ───────

def _today(**overrides):
    base = {
        "decision": {"no_garmin_today": False, "gate": {"blocked": False}, "reasons": []},
        "meals_today": 2, "budget": [], "longevity": {}, "date": "2026-09-26",
    }
    base.update(overrides)
    return base


def test_build_state_morning_no_data_confirmed_stale_is_false():
    st = vita.build_state(_today(), now_local=datetime(2026, 9, 26, 8, 0))
    assert st["time_of_day"] == "morning" and st["closed"] is False


def test_build_state_no_watch_flag():
    st = vita.build_state(_today(decision={"no_garmin_today": True, "gate": {"blocked": False}, "reasons": []}),
                          now_local=datetime(2026, 9, 26, 15, 0))
    assert st["no_watch"] is True


def test_build_state_no_food_flag():
    st = vita.build_state(_today(meals_today=0), now_local=datetime(2026, 9, 26, 15, 0))
    assert st["no_food"] is True


def test_build_state_sunday_flag():
    st = vita.build_state(_today(), now_local=datetime(2026, 9, 27, 12, 0))  # 27.09.2026 — воскресенье
    assert st["sunday"] is True


def test_build_state_evening_is_closed():
    st = vita.build_state(_today(), now_local=datetime(2026, 9, 26, 21, 30))
    assert st["closed"] is True


def test_build_state_has_decision_always_false_in_v1():
    """Ticket Часть 2: «Нужен ты» в v1 может быть всегда пустым."""
    st = vita.build_state(_today(), now_local=datetime(2026, 9, 26, 15, 0))
    assert st["has_decision"] is False


# ─────── build_gate ───────

def test_build_gate_not_blocked():
    assert vita.build_gate(_today()) == {"blocked": False}


def test_build_gate_blocked_has_label():
    today = _today(decision={"no_garmin_today": False, "gate": {"blocked": True, "condition": "Грыжа L5/S1, радикулопатия"}, "reasons": []})
    g = vita.build_gate(today)
    assert g["blocked"] is True and "щадящий режим" in g["label"]


# ─────── build_nudge — приоритет: часы > еда > вечер(тихо) > белок/темп ───────

def test_nudge_no_watch_wins_over_everything():
    st = {"no_watch": True, "no_food": True, "time_of_day": "day"}
    n = vita.build_nudge(st, {"behind_pace": True}, {"target": 160, "consumed": 10}, None)
    assert n["go"] == "watch"


def test_nudge_no_food_when_watch_ok():
    st = {"no_watch": False, "no_food": True, "time_of_day": "day"}
    n = vita.build_nudge(st, {"behind_pace": False}, {}, None)
    assert n["go"] == "protein" and "приёма" in n["text"]


def test_nudge_none_in_the_evening():
    st = {"no_watch": False, "no_food": False, "time_of_day": "evening"}
    assert vita.build_nudge(st, {"behind_pace": True}, {"target": 160, "consumed": 60}, None) is None


def test_nudge_protein_gap_mentions_real_numbers():
    st = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    n = vita.build_nudge(st, {"behind_pace": False}, {"target": 160, "consumed": 62}, None)
    assert "62" in n["text"] and "160" in n["text"]


def test_nudge_none_when_day_is_fine():
    st = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    n = vita.build_nudge(st, {"behind_pace": False}, {"target": 160, "consumed": 158}, None)
    assert n is None


# ─────── build_chips ───────

def _health(**overrides):
    base = {"metrics": []}
    base.update(overrides)
    return base


def test_chips_food_wait_when_not_logged():
    ch = vita.build_chips(_today(meals_today=0), _health(), {"summary": {"macros": {"proteins": {"consumed": None}}}})
    assert ch["food_logged"] is False and ch["protein_consumed"] is None


def test_chips_hrv_trend_from_judgment():
    health = _health(metrics=[{"key": "hrv", "value": 52, "judgment": "good"}])
    ch = vita.build_chips(_today(), health, {"summary": {"macros": {"proteins": {"consumed": "62"}}}})
    assert ch["hrv"]["trend"] == "растёт"
    assert ch["protein_consumed"] == 62.0


def test_chips_sleep_quality_is_garmin_sleep_score_not_duration():
    health = _health(metrics=[{"key": "sleep_min", "value": 436, "judgment": "good"},
                               {"key": "sleep_score", "value": 86.0, "judgment": "neutral"}])
    ch = vita.build_chips(_today(), health, {"summary": {"macros": {"proteins": {}}}})
    assert ch["sleep_min"] == 436
    assert ch["sleep_quality"] == 86.0


# ─────── _short_meal_label / _dish_sources ───────

def test_short_meal_label_extracts_meal_type_prefix():
    assert vita._short_meal_label("Завтрак: овсянка с ягодами", "08:10") == "Завтрак · 08:10"


def test_short_meal_label_falls_back_when_no_prefix():
    """Meal_description в реальных данных часто НЕ начинается со слова
    "Завтрак"/"Обед" — честный фолбэк "Приём пищи", не выдуманное название."""
    assert vita._short_meal_label("Бутерброд с ветчиной", "12:47") == "Приём пищи · 12:47"


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return self._rows


def test_dish_sources_sorted_by_contribution_descending():
    rows = [("Завтрак: омлет", 5.0, "08:00"), ("Ужин: рыба", 20.0, "19:30"), ("Обед: суп", 12.0, "13:00")]
    out = vita._dish_sources(_FakeCursor(rows), "Насыщенные жиры")
    assert [v for _, v in out] == [20.0, 12.0, 5.0]


def test_dish_sources_dedupes_repeated_meal_type_with_index():
    rows = [("Перекус 1", 3.0, "10:00"), ("Перекус 2", 4.0, "16:00")]
    # оба без явного префикса типа -> оба "Приём пищи", разойдутся по времени
    out = vita._dish_sources(_FakeCursor(rows), "Натрий")
    names = [n for n, _ in out]
    assert len(set(names)) == 2  # не схлопнулись в одну неразличимую строку


def test_dish_sources_skips_zero_and_null_values():
    rows = [("Завтрак: чай", 0.0, "08:00"), ("Обед: салат", 5.0, "13:00"), ("Ужин: суп", None, "19:00")]
    out = vita._dish_sources(_FakeCursor(rows), "Белок")
    assert len(out) == 1 and out[0][1] == 5.0


# ─────── эндпоинты: 401 без cookie, 200 с валидной, страница/логин ───────

def _cookie():
    return {va.COOKIE_NAME: va.create_session_token()}


def test_vita_today_401_without_cookie():
    r = client.get("/vita/today")
    assert r.status_code == 401


def test_vita_rhythm_401_without_cookie():
    r = client.get("/vita/rhythm")
    assert r.status_code == 401


def test_vita_today_200_with_valid_cookie():
    r = client.get("/vita/today", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("date", "state", "gate", "ring", "chips", "nudge"):
        assert key in body


def test_vita_rhythm_200_with_valid_cookie():
    r = client.get("/vita/rhythm", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("date", "state", "gate", "steps", "levers"):
        assert key in body


def test_vita_page_redirects_to_login_without_cookie():
    r = client.get("/vita", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/vita/login" in r.headers["location"]


def test_vita_page_200_with_valid_cookie():
    r = client.get("/vita", cookies=_cookie())
    assert r.status_code == 200
    assert "Vita" in r.text


def test_vita_login_page_public_no_cookie_needed():
    r = client.get("/vita/login")
    assert r.status_code == 200


def test_vita_login_wrong_password_401():
    r = client.post("/vita/login", json={"password": "неверно"})
    assert r.status_code == 401


def test_vita_login_correct_password_sets_cookie():
    r = client.post("/vita/login", json={"password": "test-vita-password-not-prod"})
    assert r.status_code == 200
    assert va.COOKIE_NAME in r.cookies


# ─────── Vita v2, этап 1 — новые поля /vita/today + новые эндпоинты ───────

def test_vita_today_has_v2_fields():
    r = client.get("/vita/today", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("chip_status", "streaks", "freezes_available", "assignments"):
        assert key in body
    assert "ahead_confidence" in body["ring"]


def test_vita_yesterday_401_without_cookie():
    r = client.get("/vita/yesterday")
    assert r.status_code == 401


def test_vita_yesterday_404_when_no_snapshot(monkeypatch):
    monkeypatch.setattr(vita, "read_day_snapshot", lambda cur, day: None)
    r = client.get("/vita/yesterday", cookies=_cookie())
    assert r.status_code == 404


def test_vita_yesterday_200_when_snapshot_exists(monkeypatch):
    monkeypatch.setattr(vita, "read_day_snapshot", lambda cur, day: {"date": str(day), "ring": {}, "chips": {}, "gate": {}})
    r = client.get("/vita/yesterday", cookies=_cookie())
    assert r.status_code == 200


def test_vita_manual_mark_401_without_cookie():
    r = client.post("/vita/manual-mark", json={"date": "2026-09-20", "field_key": "swim_happened", "value": True})
    assert r.status_code == 401


def test_vita_manual_mark_rejects_unknown_field():
    r = client.post("/vita/manual-mark", cookies=_cookie(),
                     json={"date": "2026-09-20", "field_key": "bogus", "value": True})
    assert r.status_code == 400


def test_vita_manual_mark_200_and_persists():
    r = client.post("/vita/manual-mark", cookies=_cookie(),
                     json={"date": "2026-09-20", "field_key": "swim_happened", "value": True})
    assert r.status_code == 200
    with get_conn() as conn, conn.cursor() as cur:
        marks = vita._read_manual_marks(cur, "2026-09-20")
    assert marks == {"swim_happened": True}


def test_vita_logout_clears_cookie():
    r = client.post("/vita/logout", cookies=_cookie())
    assert r.status_code == 200


def test_vita_assets_manifest_is_public():
    r = client.get("/vita-assets/manifest.webmanifest")
    assert r.status_code == 200


# ─────── ring/day_issue сборка не смешивает bioage-контур c PhenoAge ───────
# (ПЛАН СБОРКИ п.3: «Кольцо Today = дневной вклад... Me = PhenoAge — разные
# числа, нигде не смешивать» — здесь Vita v1 вообще не читает PhenoAge/Me).

def test_build_today_ring_has_no_phenoage_fields():
    r = client.get("/vita/today", cookies=_cookie())
    ring = r.json()["ring"]
    assert "phenoage" not in ring and "chrono_age" not in ring


# =====================================================================
# Vita v2, этап 2 (2026-09-28) — «Проверки»: /vita/checks, /vita/questions/
# resolve, /vita/cases/{id}/evidence + новые поля /vita/today. Бизнес-логика
# самого агрегатора — tests/test_checks.py; здесь только маршрутизация/
# авторизация/интеграция с реальной (пустой) card_test БД.
# =====================================================================

def test_vita_today_has_checks_fields():
    r = client.get("/vita/today", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    assert "inbox" in body and "checks_summary" in body
    assert body["inbox"] == []  # card_test пуст между тестами
    assert body["checks_summary"] is None


def test_vita_checks_401_without_cookie():
    r = client.get("/vita/checks")
    assert r.status_code == 401


def test_vita_checks_200_empty_on_clean_db():
    r = client.get("/vita/checks", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"questions": 0, "checks": 0, "habits": 0}


def test_vita_checks_mode_filter():
    r = client.get("/vita/checks", params={"mode": "habits"}, cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "habits"
    assert body["items"] == []


def test_vita_checks_invalid_mode_400():
    r = client.get("/vita/checks", params={"mode": "bogus"}, cookies=_cookie())
    assert r.status_code == 400


def test_vita_questions_resolve_401_without_cookie():
    r = client.post("/vita/questions/resolve", json={
        "question_id": "cq_dg_x", "source": "disagreement", "action": "decline", "title": "тест",
    })
    assert r.status_code == 401


def test_vita_questions_resolve_disagreement_roundtrip():
    import json as _json
    from ulid import ULID as _ULID

    dis_id = f"dg_{_ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.disagreement (id, ts_event, provenance, opinion_doctor, opinion_advisor, "
            "significance, status) VALUES (%s, now(), %s, 'A', 'Б', 'закрывается: тест', 'raised')",
            (dis_id, _json.dumps({"origin": "test"})),
        )
        conn.commit()
    qid = f"cq_{dis_id}"

    r = client.get("/vita/checks", params={"mode": "questions"}, cookies=_cookie())
    assert qid in [q["id"] for q in r.json()["items"]]

    r = client.post("/vita/questions/resolve", cookies=_cookie(), json={
        "question_id": qid, "source": "disagreement", "action": "decline", "title": "тест", "reason": "не сейчас",
    })
    assert r.status_code == 200
    assert r.json()["decision"] == "decline"

    r = client.get("/vita/checks", params={"mode": "questions"}, cookies=_cookie())
    assert qid not in [q["id"] for q in r.json()["items"]]


def test_vita_questions_resolve_detective_check_uses_real_separate_connections():
    """Регрессия на живой баг (2026-09-28, поймано ручной проверкой на проде):
    resolve_question() регистрирует metric_coverage через СВОЙ курсор, а
    propose_recommendation() открывает ОТДЕЛЬНОЕ соединение — если регистрация
    не закоммичена ДО этого вызова, G2 (gates.py) её не видит и тихо
    понижает frequency-ожидание до unmeasurable. tests/test_checks.py не
    ловит это (там ВСЕ get_conn() под _isolate_real_schema_writes схлопнуты
    в одно физическое соединение — баг невоспроизводим); только здесь, через
    настоящий TestClient с настоящими закоммиченными записями, два вызова
    get_conn() внутри одного запроса — действительно два разных соединения,
    как в проде."""
    import json as _json
    from ulid import ULID as _ULID

    problem_id = f"pb_{_ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.problem (id, ts_event, provenance, title, status, opened_ts) "
            "VALUES (%s, now(), %s, 'Кейс регрессии частоты', 'active', now())",
            (problem_id, _json.dumps({"origin": "test"})),
        )
        conn.commit()
    qid = f"dq_{problem_id}_тест-фактор_0"

    r = client.post("/vita/questions/resolve", cookies=_cookie(), json={
        "question_id": qid, "source": "detective", "action": "check", "title": "Тестовая частотная проверка",
        "problem_id": problem_id, "factor": "тест-фактор", "lag_days": 0,
    })
    assert r.status_code == 200
    rec_id = r.json()["created_rec_id"]
    assert rec_id is not None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT type, metric_key FROM {schema()}.expectation WHERE rec_id = %s", (rec_id,))
        ex_type, metric_key = cur.fetchone()
    assert ex_type == "frequency", "рекомендация не должна тихо падать в unmeasurable — метрика уже зарегистрирована"
    assert metric_key == f"episode_count:{problem_id}"


def test_vita_questions_resolve_unknown_source_400():
    r = client.post("/vita/questions/resolve", cookies=_cookie(), json={
        "question_id": "xx_1", "source": "bogus", "action": "check", "title": "тест",
    })
    assert r.status_code == 400


def test_vita_case_evidence_401_without_cookie():
    r = client.get("/vita/cases/pb_x/evidence")
    assert r.status_code == 401


def test_vita_case_evidence_404_unknown_problem():
    r = client.get("/vita/cases/pb_does_not_exist/evidence", cookies=_cookie())
    assert r.status_code == 404


def test_vita_case_evidence_200_for_real_problem():
    import json as _json
    from ulid import ULID as _ULID

    problem_id = f"pb_{_ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.problem (id, ts_event, provenance, title, status, opened_ts) "
            "VALUES (%s, now(), %s, 'Кейс роута тест', 'active', now())",
            (problem_id, _json.dumps({"origin": "test"})),
        )
        conn.commit()
    r = client.get(f"/vita/cases/{problem_id}/evidence", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["problem_id"] == problem_id
    assert body["episodes"] == []


# =====================================================================
# Часть 3 тикета — четыре обязательных состояния, API-уровнем, на
# полной сборке build_today()/build_rhythm() (не только build_state()).
# =====================================================================

class _FC:
    """Курсор-заглушка для build_levers/_dish_sources внутри build_today —
    возвращает пустые результаты (эти тесты не проверяют разбор блюд)."""
    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return None


def _patch_dashboards(monkeypatch, *, no_garmin=False, meals_today=2, gate_blocked=False,
                      reasons=None, metrics=None, protein_consumed="90"):
    today = {
        "date": "2026-09-26",
        "decision": {
            "no_garmin_today": no_garmin,
            "gate": {"blocked": gate_blocked, "condition": "Грыжа L5/S1, радикулопатия" if gate_blocked else None},
            "reasons": reasons or [],
        },
        "meals_today": meals_today,
        "budget": [],
        "streaks": [],
        "longevity": {"affects_today_total": 0.0085},
    }
    health = {"metrics": metrics or []}
    tn = {"summary": {"macros": {"proteins": {"consumed": protein_consumed, "target": "160"}}}}
    monkeypatch.setattr(vita, "get_today_dashboard", lambda cur: today)
    monkeypatch.setattr(vita, "get_health_dashboard", lambda cur: health)
    monkeypatch.setattr(vita, "get_today_nutrition", lambda cur: tn)
    # Vita v2, этап 1: build_today также зовёт _fetch_streak_inputs/read_chip_norm
    # (нужны cur.description/настоящую БД) — эти 4 теста собирают build_today()
    # целиком на _FC() (без description), не через реальный курсор, поэтому
    # мокаем и их тем же способом, что остальные источники выше.
    monkeypatch.setattr(vita, "_fetch_streak_inputs", lambda cur: ([], [], []))
    monkeypatch.setattr(vita, "read_chip_norm", lambda cur: dict(vita.DEFAULT_CHIP_NORM))


def test_state_morning_no_data_looks_calm_not_failing(monkeypatch):
    """Часть 3: утро — «предстоит», спокойный, ничего не выглядит провально."""
    _patch_dashboards(monkeypatch, meals_today=1, metrics=[{"key": "sleep_min", "value": 424, "judgment": "good"}])
    monkeypatch.setattr(vita.timeutil, "now_local", lambda: datetime(2026, 9, 26, 8, 0))
    out = vita.build_today(_FC())
    assert out["state"]["time_of_day"] == "morning"
    assert out["state"]["no_watch"] is False
    # утро НЕ показывает жёсткий "0"/провал — ring остаётся содержательным числом или None, не путается с "0 = плохо"
    assert out["ring"]["score"] != 0 or out["ring"]["score"] is None


def test_state_no_watch_has_no_fake_zeros(monkeypatch):
    """Часть 3: день без часов — «день не оценивается», нудж «часы не видели
    тебя», НОЛЬ ложных нулей (null≠0 теперь и в UI)."""
    _patch_dashboards(monkeypatch, no_garmin=True, meals_today=1)
    monkeypatch.setattr(vita.timeutil, "now_local", lambda: datetime(2026, 9, 26, 12, 0))
    out = vita.build_today(_FC())
    assert out["state"]["no_watch"] is True
    assert out["ring"]["score"] is None  # НЕ 0
    assert out["chips"]["sleep_min"] is None  # НЕ 0
    assert out["chips"]["hrv"]["value"] is None
    assert out["chips"]["energy"] is None
    assert out["nudge"]["go"] == "watch"


def test_state_no_food_is_neutral_waiting(monkeypatch):
    """Часть 3: еда не записана — рычаги «ждёт первого приёма», нейтрально."""
    _patch_dashboards(monkeypatch, meals_today=0, protein_consumed=None)
    monkeypatch.setattr(vita.timeutil, "now_local", lambda: datetime(2026, 9, 26, 14, 0))
    out = vita.build_today(_FC())
    assert out["state"]["no_food"] is True
    assert out["chips"]["food_logged"] is False
    assert out["chips"]["protein_consumed"] is None
    assert out["nudge"]["go"] == "protein"
    rhythm = vita.build_rhythm(_FC())
    for key in ("fat", "sodium", "protein"):
        assert rhythm["levers"][key]["has_data"] is False


def test_state_normal_day_has_real_numbers(monkeypatch):
    """Часть 3: обычный день — как в макете, реальные числа везде."""
    _patch_dashboards(
        monkeypatch, meals_today=3,
        metrics=[{"key": "sleep_min", "value": 424, "judgment": "good"},
                 {"key": "hrv", "value": 52, "judgment": "good"},
                 {"key": "body_battery", "value": 77, "judgment": "good"}],
    )
    monkeypatch.setattr(vita.timeutil, "now_local", lambda: datetime(2026, 9, 26, 16, 2))
    out = vita.build_today(_FC())
    assert out["state"] == {
        "time_of_day": "day", "no_watch": False, "no_food": False,
        "sunday": False, "closed": False, "has_decision": False,
    }
    # Живая правка (2026-09-28, «индекс дня зависит от кругляшей»): «Заряд»
    # берёт РЕАЛЬНЫЙ Body Battery=77 (не судейские 100, хотя ни одного bad/
    # warn в сценарии), «Сон» без своей Оценки сна откатывается на судейский
    # 100, Движение/Питание без данных не считаются вовсе — среднее (77+100)/2=88.5,
    # round() к чётному -> 88.
    assert out["ring"]["score"] == 88
    assert out["chips"]["sleep_min"] == 424
    assert out["chips"]["hrv"]["value"] == 52
    assert out["chips"]["energy"] == 77
    assert out["chips"]["protein_consumed"] == 90.0


# =====================================================================
# Vita v2, этап 2 — GET /vita/topic/{segment} (живые данные, реальный DB)
# =====================================================================

def test_vita_topic_401_without_cookie():
    r = client.get("/vita/topic/recovery")
    assert r.status_code == 401


def test_vita_topic_404_unknown_segment():
    r = client.get("/vita/topic/bogus", cookies=_cookie())
    assert r.status_code == 404


def test_vita_topic_recovery_200_has_expected_keys():
    r = client.get("/vita/topic/recovery", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("hrv_last", "hrv_avg7", "hrv_avg30", "hrv_history", "resting_hr", "acwr", "coach", "publications"):
        assert key in body


def test_vita_topic_sleep_200_has_expected_keys():
    r = client.get("/vita/topic/sleep", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("last_night_min", "avg14_min", "delta_min", "sleep_score", "history",
                "light_min", "deep_min", "rem_min", "awake_min", "coach", "publications"):
        assert key in body


def test_vita_topic_move_200_has_expected_keys():
    r = client.get("/vita/topic/move", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    for key in ("steps_now", "steps_target", "acwr", "acwr_status", "workouts", "history_daily",
                "coach", "publications"):
        assert key in body


def test_vita_topic_food_200_has_publications():
    r = client.get("/vita/topic/food", cookies=_cookie())
    assert r.status_code == 200
    body = r.json()
    assert "publications" in body and "micro_heatmap" in body


# ─────── сверка с макетом v7 (2026-09-28): короткое действие, полоса серий, неделя, «Врач», «Я» ───────

def test_nudge_has_short_headline_for_home():
    """Заголовок главной — одно короткое действие (макет v7), полный текст — в шторке."""
    day = {"no_watch": False, "no_food": False, "time_of_day": "day"}
    for st, steps, protein in (
        ({**day, "no_watch": True}, {}, {}),
        ({**day, "no_food": True}, {}, {}),
        (day, {"behind_pace": True}, {"target": 160, "consumed": 60}),
        (day, {"behind_pace": False}, {"target": 160, "consumed": 60}),
        (day, {"behind_pace": True}, {"target": 160, "consumed": 158}),
    ):
        n = vita.build_nudge(st, steps, protein, None)
        assert n["short"] and len(n["short"]) <= 28


def test_build_streaks_strip_marks_days():
    days = [f"2026-09-{d:02d}" for d in range(1, 21)]
    gap_day = days[-3]
    rows = [_trend_row(d, sleep_min=(300 if d == gap_day else 450)) for d in days if d != days[-5]]
    result = vita.build_streaks(rows, [], [], days[-1])
    s = next(x for x in result["streaks"] if x["key"] == "sleep_zone")
    strip = s["days"]
    assert len(strip) == vita.STREAK_STRIP_DAYS
    codes = {d["date"]: d["c"] for d in strip}
    assert codes[gap_day] == "f"       # провал закрыт заморозкой
    assert codes[days[-1]] == "t"      # сегодня ещё идёт
    assert days[-5] not in codes       # дня без строки в истории в полосе нет
    assert codes[days[-2]] == "y"


def test_summarize_week_counts_green_days():
    from datetime import date
    snaps = [(date(2026, 9, d), {"score": s}) for d, s in ((21, 85), (22, 79), (23, 90), (24, None), (25, 81))]
    w = vita.summarize_week(snaps)
    assert w["days"] == 4 and w["green"] == 3 and w["avg"] == 84


def test_summarize_week_empty_is_none():
    assert vita.summarize_week([]) is None


def test_shape_doctor_uses_latest_completed_report_and_first_panel():
    reports = [
        {"id": "cs_2", "status": "empty", "ts_recorded": "2026-09-27T10:00:00", "topic": "x"},
        {"id": "cs_1", "status": "completed", "ts_recorded": "2026-09-22T10:00:00", "topic": "Общий профиль",
         "question": "?", "roles": ["кардиолог"], "actions": [{"imperative": "Сдать ApoB", "accepted": True}],
         "emerging": [{"method": "Омега-3 индекс", "maturity": "когортное"}], "skeptic_notes": ["мало данных"]},
    ]
    plan = {"panels": [
        {"date": "2026-10-12", "n_markers": 2, "fasting_required": True, "tube_types": ["EDTA"], "total_price_rub": None,
         "export_text": "…", "shifted": [],
         "markers": [{"name": "ApoB", "why": "серия по жирам", "category": "Липиды", "fasting_required": True, "source_type": "consilium"},
                     {"name": "B12", "why": "", "category": "Витамины", "fasting_required": False, "source_type": "visit"}]},
        {"date": "2026-12-15", "n_markers": 1, "markers": [], "fasting_required": False, "tube_types": []},
    ], "conflicts": []}
    d = vita.shape_doctor(reports, [{"date": "2026-09-22", "category": "невролог", "note": "B12"}], [], plan)
    assert d["consilium"]["id"] == "cs_1" and d["consilium"]["actions"][0]["text"] == "Сдать ApoB"
    assert d["consilium_count"] == 1
    assert d["next_draw"]["date"] == "2026-10-12" and d["next_draw"]["n"] == 2 and d["next_draw"]["price_rub"] is None
    assert [x["date"] for x in d["draws"]] == ["2026-10-12", "2026-12-15"]


def test_shape_doctor_empty_sources():
    d = vita.shape_doctor([], [], [], {"panels": []})
    assert d["consilium"] is None and d["next_draw"] is None and d["draws"] == []


def test_shape_me_drivers_sorted_and_systems_grouped():
    bio = {
        "phenoage": {"value": 32.4, "chrono_age": 41.0, "delta": -8.6, "date": "2026-08-01"},
        "drivers": [{"label": "Хроно", "years": 41, "type": "total"}, {"label": "СРБ", "years": 0.9, "type": "pos"},
                    {"label": "Альбумин", "years": -2.1, "type": "neg"}, {"label": "PhenoAge", "years": 32.4, "type": "total"}],
        "history": [{"date": "2026-02-01", "phenoage": 34.0}, {"date": "2026-08-01", "phenoage": 32.4}],
        "biomarkers": [
            {"label": "АСТ", "group": "Печень", "value": 41, "in_lab_range": False, "in_opt_range": False},
            {"label": "АЛТ", "group": "Печень", "value": 38, "in_lab_range": True, "in_opt_range": True},
            {"label": "СРБ", "group": "Воспаление", "value": 2.6, "in_lab_range": True, "in_opt_range": False},
            {"label": "Глюкоза", "group": "Метаболизм", "value": 4.8, "in_lab_range": True, "in_opt_range": True},
            {"label": "Пусто", "group": "Метаболизм", "value": None},
        ],
    }
    me = vita.shape_me(bio)
    assert [d["label"] for d in me["drivers"]] == ["Альбумин", "СРБ"]
    assert [(s["name"], s["status"]) for s in me["systems"]] == [("Печень", "out"), ("Воспаление", "watch"), ("Метаболизм", "ok")]
    assert me["systems"][0]["flagged"] == ["АСТ"] and me["systems"][2]["n"] == 1
    assert me["phenoage"]["delta"] == -8.6 and len(me["history"]) == 2


def test_new_vita_tabs_require_session():
    for path in ("/vita/doctor", "/vita/me", "/vita/medpassport"):
        assert client.get(path).status_code == 401


# ─────── «Питание» — сверка с макетом v7 (2026-09-29) ───────

def test_build_macros_none_without_food_and_pairs_with_food():
    tn = {"summary": {"calories": {"consumed": 1640, "target": "2300"},
                      "macros": {"proteins": {"consumed": 96, "target": "160"}, "fats": {"consumed": 54.2, "target": "75"},
                                 "carbs": {"consumed": 186, "target": "250"}}}}
    assert vita.build_macros(tn, False) is None
    m = vita.build_macros(tn, True)
    assert m["kcal"] == {"consumed": 1640, "target": 2300}
    assert m["protein"]["target"] == 160 and m["fat"]["consumed"] == 54.2 and m["carbs"]["target"] == 250


def test_week_top_sources_sums_days_and_ranks():
    src = {"byDay": [[{"name": "Суп", "pct": 20}, {"name": "Рыба", "pct": 30}], [], [{"name": "Суп", "pct": 25}], [{"name": "Салат", "pct": 5}]]}
    assert vita._week_top_sources(src) == ["Суп", "Рыба", "Салат"]
    assert vita._week_top_sources(None) == []


def test_shape_food_topic_carries_everything_the_sheet_shows():
    weekly = {
        "days": ["2026-09-22", "2026-09-23"],
        "heatmap": [{"label": "Йод", "avgPct": 45, "level": 2, "unit": "мкг", "values": [40, 50], "note": "морепродукты"}],
        "normal": [{"label": "Клетчатка"}, {"label": "Калий"}],
        "sources": {"Йод": {"byDay": [[{"name": "Треска", "pct": 30}], []]}},
        "diet_quality": {"ahei": {"week_avg": 71, "target": 80, "max": 110}, "plants": {"count": 43, "target": 30}},
    }
    y = {"meals": [{"t": "08:10", "d": "Омлет", "p": 26, "k": 350}], "protein": 26, "kcal": 350}
    out = vita.shape_food_topic(weekly, y, [])
    h = out["micro_heatmap"][0]
    assert h["days_pct"] == [40, 50] and h["note"] == "морепродукты" and h["top_sources"] == ["Треска"]
    assert out["normal"] == ["Клетчатка", "Калий"] and out["days"] == ["2026-09-22", "2026-09-23"]
    assert out["diet_quality"] == {"ahei_week": 71, "ahei_target": 80, "ahei_max": 110, "plants": 43, "plants_target": 30}
    assert out["yesterday"]["protein"] == 26


def test_shape_food_topic_diet_quality_error_is_none():
    out = vita.shape_food_topic({"diet_quality": {"error": "boom"}}, {"meals": [], "protein": 0, "kcal": 0}, [])
    assert out["diet_quality"] is None and out["micro_heatmap"] == [] and out["normal"] == []


def test_sugar_is_a_limit_lever_like_fat_and_sodium():
    assert vita._LEVER_META["sugar"]["budget_label"] == "Добавленный сахар"
    today = {"meals_today": 0, "budget": [], "streaks": []}
    out = vita.build_levers(_FakeCursor([]), today, {"summary": {}})
    assert out["sugar"] == {"has_data": False}


def test_yesterday_meals_sums_protein_kcal_and_sat_fat():
    rows = [("08:10", "Омлет ", 26, 380, 6.5), ("19:50", "Треска", 36, "410", None)]
    y = vita._yesterday_meals(_FakeCursor(rows))
    assert y["protein"] == 62 and y["kcal"] == 790 and y["sat_fat"] == 6.5
    assert y["meals"][0] == {"t": "08:10", "d": "Омлет", "p": 26, "k": 380}


# ─────── точка на вкладке «Врач» (2026-09-30) ───────

def test_draw_due_true_when_first_panel_is_today_or_overdue():
    assert vita.draw_due({"panels": [{"date": "2026-09-30"}]}, "2026-09-30") is True
    assert vita.draw_due({"panels": [{"date": "2026-09-01"}]}, "2026-09-30") is True


def test_draw_due_false_when_panel_in_future_or_no_panels():
    assert vita.draw_due({"panels": [{"date": "2026-10-23"}]}, "2026-09-30") is False
    assert vita.draw_due({"panels": []}, "2026-09-30") is False
    assert vita.draw_due({}, "2026-09-30") is False


# ─────── шторка «Сдача» по макету v5 (2026-09-30) ───────

def test_iv_label_formats():
    assert vita._iv_label(90, False) == "90 дн"
    assert vita._iv_label(180, False) == "6 мес"
    assert vita._iv_label(365, False) == "1 год"
    assert vita._iv_label(730, False) == "2 года"
    assert vita._iv_label(None, False) == ""
    assert vita._iv_label(365, True) == "однократно"


def test_marker_reason_strips_catalog_purpose():
    from app.lab_catalog import LAB_CATALOG
    purpose = LAB_CATALOG["M035"]["purpose"]
    assert vita._marker_reason("M035", f"{purpose} — онемение в руке") == "онемение в руке"
    assert vita._marker_reason("M035", purpose) == ""


def test_draw_groups_have_reason_count_and_fixed_order():
    ms = [
        {"group": "Плановый мониторинг", "due": "2020-06-17", "iv": "1 год", "reason": ""},
        {"group": "Панель PhenoAge", "due": "2024-11-14", "iv": "6 мес", "reason": ""},
        {"group": "Панель PhenoAge", "due": "2027-02-02", "iv": "6 мес", "reason": ""},
        {"group": "Назначил врач", "due": "2026-09-30", "iv": "", "reason": "онемение в руке"},
    ]
    g = vita._draw_groups(ms, "2026-09-30")
    assert [x["name"] for x in g] == ["Панель PhenoAge", "Назначил врач", "Плановый мониторинг"]
    assert g[0]["sh"] == "пришёл срок · раз в 6 мес" and g[0]["n"] == 2
    assert g[1]["sh"] == "онемение в руке"
    assert g[2]["sh"] == "срок был в июне 2020"


def test_next_note_first_middle_last():
    ps = [{"date": "2026-10-12"}, {"date": "2026-12-15"}, {"date": "2027-02-10"}]
    assert vita._next_note(ps, 0) == "Больше ничего не нужно. Следующая плановая сдача — не раньше 15 декабря."
    assert vita._next_note(ps, 1) == "Следующая — 10 февраля."
    assert "ничего нет" in vita._next_note(ps, 2)


def test_shape_doctor_next_draw_carries_groups_and_note():
    plan = {"panels": [
        {"date": "2026-10-12", "n_markers": 1, "fasting_required": False, "tube_types": [], "shifted": [],
         "markers": [{"code": "M035", "name": "Витамин D", "why": "", "category": "Дефициты",
                      "fasting_required": False, "source_type": "standing", "natural_due_date": "2026-10-12"}]},
        {"date": "2026-12-15", "n_markers": 0, "markers": [], "fasting_required": False, "tube_types": []},
    ], "conflicts": []}
    nd = vita.shape_doctor([], [], [], plan)["next_draw"]
    assert nd["groups"][0]["name"] == "Плановый мониторинг" and nd["markers"][0]["iv"] == "6 мес"
    assert "15 декабря" in nd["next_note"]


def test_breakdown_limit_detail_has_no_trailing_zero():
    today = {"decision": {"reasons": []}, "budget": [
        {"kind": "limit", "status": "ok", "label": "Кофеин", "consumed": 80.0, "cap": 400.0, "unit": "мг"}]}
    rows = vita.build_index_breakdown(today, {"metrics": []})
    assert rows[0]["detail"] == "80/400 мг"


def test_shape_food_topic_carries_upper_limit_for_red_cells():
    weekly = {"heatmap": [{"label": "Цинк", "avgPct": 110, "level": 3, "unit": "мг", "values": [90, 250], "upperBoundPct": 300,
                           "note": None}], "sources": {}}
    out = vita.shape_food_topic(weekly, {"meals": [], "protein": 0, "kcal": 0}, [])
    assert out["micro_heatmap"][0]["upper_pct"] == 300


# ─────── режим поездки (2026-09-30): серии питания на паузе, сон как обычно ───────

def test_travel_pauses_food_streak_without_burning_freeze_or_breaking():
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]
    rows = [_trend_row(d) for d in days]
    meals = [{"Date": f"{d}T08:00", "Натрий": ("9000" if d in days[-4:-1] else "500")} for d in days[:-1]]
    trip = set(days[-4:])  # три «провала» солью внутри поездки + сегодня
    with_trip = vita.build_streaks(rows, meals, _SODIUM_TARGET, days[-1], trip)
    na = next(s for s in with_trip["streaks"] if s["key"] == "Натрий")
    assert na["count"] == 6 and na["status"] == "paused"
    assert all(d["c"] == "p" for d in na["days"] if d["date"] in trip)
    assert with_trip["freezes_available"] == vita.STREAK_FREEZE_POOL  # провалы в поездке заморозок не тратят
    no_trip = vita.build_streaks(rows, meals, _SODIUM_TARGET, days[-1])
    assert next(s for s in no_trip["streaks"] if s["key"] == "Натрий")["count"] != 6


def test_travel_does_not_touch_sleep_streak():
    days = [f"2026-09-{d:02d}" for d in range(1, 8)]
    rows = [_trend_row(d) for d in days]
    res = vita.build_streaks(rows, [], [], days[-1], set(days))
    s = next(x for x in res["streaks"] if x["key"] == "sleep_zone")
    assert s["count"] == 7


def test_travel_state_chain_from_today():
    st = vita.travel_state({"2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"}, "2026-09-30")
    assert st == {"active": True, "until": "2026-10-02", "days_left": 3}
    assert vita.travel_state({"2026-10-01"}, "2026-09-30") == {"active": False, "until": None, "days_left": 0}


def test_start_and_end_travel_roundtrip_keeps_past_days():
    today = date(2026, 9, 30)
    with get_conn() as conn, conn.cursor() as cur:
        vita.write_travel_day(cur, date(2026, 9, 29), True)
        vita.start_travel(cur, today, 3)
        assert vita.travel_state(vita.read_travel_days(cur), "2026-09-30")["until"] == "2026-10-02"
        vita.end_travel(cur, today)
        left = vita.read_travel_days(cur)
    assert "2026-09-29" in left and "2026-09-30" not in left and "2026-10-01" not in left


def test_vita_travel_endpoints_401_and_validation():
    assert client.post("/vita/travel", json={"days": 3}).status_code == 401
    assert client.post("/vita/travel/end").status_code == 401
    assert client.post("/vita/travel", cookies=_cookie(), json={"days": 0}).status_code == 422
    assert client.post("/vita/travel", cookies=_cookie(), json={"days": 999}).status_code == 422


def test_vita_travel_endpoint_start_then_end():
    r = client.post("/vita/travel", cookies=_cookie(), json={"days": 7})
    assert r.status_code == 200 and r.json()["travel"]["active"] and r.json()["travel"]["days_left"] == 7
    r = client.post("/vita/travel/end", cookies=_cookie())
    assert r.status_code == 200 and r.json()["travel"]["active"] is False


# ─────── недельная сетка привычек врача (макет v5, «Привычки») ───────

class _SeqCursor:
    def __init__(self, *results):
        self._results = list(results)
        self._cur = []

    def execute(self, *a, **k):
        self._cur = self._results.pop(0)

    def fetchall(self):
        return self._cur


def _rx(today_iso, trend_rows, marks, steps_now=None, target=8000):
    cur = _SeqCursor(trend_rows, marks)
    out = vita.build_rx_week(cur, today_iso, steps_now, target)
    return {i["k"]: [d["c"] for d in i["days"]] for i in out["items"]}, out


def test_rx_week_reads_previous_day_from_next_row_and_marks_future():
    # среда 2026-09-30: Пн=28, Вт=29. Значение дня d лежит в строке d+1.
    rows = [(date(2026, 9, 29), "Да", 30, 9000), (date(2026, 9, 30), "Нет", 90, 5000)]
    cells, out = _rx("2026-09-30", rows, [])
    assert cells["swim"][:3] == ["y", "", "t"] and cells["swim"][3:] == ["f"] * 4
    assert cells["move"][:2] == ["y", ""]
    assert cells["walk"][:2] == ["y", ""]
    assert next(i for i in out["items"] if i["k"] == "swim")["n"] == 1


def test_rx_week_no_row_is_no_data_and_manual_mark_overrides():
    cells, _ = _rx("2026-09-30", [], [(date(2026, 9, 30), "swim_happened", True), (date(2026, 9, 28), "movement_ok", False)])
    assert cells["swim"][:3] == ["q", "q", "y m"]
    assert cells["move"][0] == ""  # ручное «нет» перекрывает отсутствие данных


def test_rx_week_today_walk_uses_live_steps():
    cells, _ = _rx("2026-09-30", [], [], steps_now=8500)
    assert cells["walk"][2] == "y" and cells["move"][2] == "t"
    cells, _ = _rx("2026-09-30", [], [], steps_now=3000)
    assert cells["walk"][2] == "t"


def test_draw_group_reason_is_clipped_on_word_boundary():
    assert vita._clip("Пересдать печёночный профиль с дробным билирубином, АЛТ, АСТ, ГГТ через 4–6 недель", 60).endswith("…")
    out = vita._clip("Пересдать печёночный профиль с дробным билирубином, АЛТ, АСТ, ГГТ через 4–6 недель", 60)
    assert not out.rstrip("…").endswith((" че", " чер")) and len(out) <= 61
    assert vita._clip("коротко", 60) == "коротко"


# ─────── медпаспорт по системам: грейды (макет v5) ───────

def test_lab_grade_four_levels():
    g = vita.lab_grade
    assert g(10, 5, 8, None, None) == "out"            # выше референса
    assert g(2, 5, 8, None, None) == "out"             # ниже референса
    assert g(6, 5, 8, None, None) == "normal"          # оптимум не задан
    assert g(7.5, 5, 8, 5.5, 7) == "watch"             # в референсе, вне цели
    assert g(6, 5, 8, 5.5, 7) == "exc"                 # внутри цели, цель строже референса
    assert g(6, 5, 8, 5, 8) == "normal"                # цель = референс → не «отлично»
    assert g(None, 5, 8, 5.5, 7) is None


def test_lab_grade_one_sided_reference():
    assert vita.lab_grade(0.5, None, 5, None, 1) == "exc"      # СРБ: реф. < 5, цель < 1
    assert vita.lab_grade(2.6, None, 5, None, 1) == "watch"
    assert vita.lab_grade(6, None, 5, None, 1) == "out"


def test_shape_labs_groups_sorted_and_zero_lower_bound_is_one_sided():
    bm = [
        {"label": "Холестерин не-ЛПВП", "group": "Липидный профиль", "value": 3.9, "unit": "ммоль/л",
         "measured_date": "2026-08-14", "lab_min": 0.0, "lab_max": 3.8, "opt_min": 0.0, "opt_max": 3.4},
        {"label": "Глюкоза", "group": "Углеводный обмен", "value": 5.0, "unit": "ммоль/л",
         "measured_date": "2026-08-06", "lab_min": 3.9, "lab_max": 6.0, "opt_min": 4.2, "opt_max": 5.0},
    ]
    other = [{"marker": "АЧТВ", "value": 32.0, "unit": "сек", "ref_min": None, "ref_max": None, "date": "2026-08-06"}]
    out = vita.shape_labs_by_system(bm, other, {})
    assert [g["name"] for g in out] == ["Липидный профиль", "Углеводный обмен", "Прочее"]  # худшая группа первой, «Прочее» в конце
    lip = out[0]
    assert lip["worst"] == "out" and lip["ok"] == 0
    assert "реф. < 3.8" in lip["items"][0]["text"] and "цель < 3.4" in lip["items"][0]["text"]  # 0–X → «< X»
    assert out[1]["items"][0]["grade"] == "exc"
    assert out[2]["items"][0]["grade"] == "normal"


def test_shape_labs_skips_markers_already_in_biomarkers_and_dash_unit():
    bm = [{"label": "МНО", "group": "Коагулограмма", "value": 1.1, "unit": "-", "measured_date": "2026-08-06",
           "lab_min": None, "lab_max": 1.3, "opt_min": None, "opt_max": None}]
    dup = [{"marker": "МНО", "value": 1.1, "unit": "-", "ref_min": None, "ref_max": 1.3, "date": "2026-08-06"}]
    out = vita.shape_labs_by_system(bm, dup, {})
    assert len(out) == 1 and out[0]["n"] == 1
    assert out[0]["items"][0]["text"] == "1.1 · реф. < 1.3"
