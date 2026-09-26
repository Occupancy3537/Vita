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


# ─────── _time_of_day ───────

def test_time_of_day_morning():
    assert vita._time_of_day(datetime(2026, 9, 26, 8, 0)) == "morning"


def test_time_of_day_day():
    assert vita._time_of_day(datetime(2026, 9, 26, 16, 2)) == "day"


def test_time_of_day_evening():
    assert vita._time_of_day(datetime(2026, 9, 26, 21, 30)) == "evening"


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
    assert out["ring"]["score"] == 100  # ни одного bad/warn критерия в этом сценарии
    assert out["chips"]["sleep_min"] == 424
    assert out["chips"]["hrv"]["value"] == 52
    assert out["chips"]["protein_consumed"] == 90.0
