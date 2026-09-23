"""app/dashboard.py::get_today_dashboard — порт n8n `today-dashboard (cache)` /
Build Today JSON (2026-09-20). health.daily_trends/meals/nutrient_targets/
recommendations_log/phenoage_log/patient_state/action_log/user_profile — общая
прод-схема, только чтение (тот же принцип, что test_dashboard_bioage.py) —
юниты на чистую логику (гейт нагрузки, парсер ACTIONS, бюджет) мокают вход,
форма ответа проверяется на реальных данных."""
from fastapi.testclient import TestClient

from app.dashboard import (
    _ALCOHOL_TRACE_THRESHOLD_G,
    _action_id,
    _alcohol_effective_g,
    _garmin_data_confirmed_stale,
    _hhmm,
    _load_gate,
    _parse_actions,
    _parse_target_num,
    _round4,
    get_today_dashboard,
)
from app.main import app

client = TestClient(app)


# --- _garmin_data_confirmed_stale: живая жалоба 2026-09-24 (забыл часы) ----

def test_garmin_stale_no_gap_same_date_never_stale():
    """last_date == today_iso — нечего подтверждать, свежие данные есть."""
    assert _garmin_data_confirmed_stale("2026-09-24", "2026-09-24", 9) is False
    assert _garmin_data_confirmed_stale("2026-09-24", "2026-09-24", 20) is False


def test_garmin_stale_gap_before_window_close_not_yet_confirmed():
    """Разрыв дат утром/днём — окно попыток garminbot (07:00-16:00 ВЛ) ещё
    не закрылось, "ещё не пришло" — не то же самое, что "не придёт"."""
    assert _garmin_data_confirmed_stale("2026-09-23", "2026-09-24", 9) is False
    assert _garmin_data_confirmed_stale("2026-09-23", "2026-09-24", 15) is False


def test_garmin_stale_gap_after_window_close_confirmed():
    """После закрытия окна (>=16:00 ВЛ) разрыв дат — подтверждённый пропуск
    (живой случай: часы забыли надеть на ночь)."""
    assert _garmin_data_confirmed_stale("2026-09-23", "2026-09-24", 16) is True
    assert _garmin_data_confirmed_stale("2026-09-23", "2026-09-24", 22) is True


# --- _load_gate: инвариант безопасности (грыжа L5/S1), A6 fail-safe --------

def test_load_gate_active_restriction_no_swim():
    pstate = [{"Status": "active", "Contra_Load": "бег, прыжки", "Confirmed_Date": "2026-06-26",
               "Condition": "Грыжа L5/S1", "Allowed": "ходьба", "Provokers": "сидение",
               "Review_Due": "2026-10-06", "Source": "МРТ"}]
    gate = _load_gate(pstate, {})
    assert gate == {
        "cap": 1, "blocked": True, "condition": "Грыжа L5/S1", "contra": "бег, прыжки",
        "allowed": "ходьба", "provokers": "сидение", "review_due": "2026-10-06",
        "source": "МРТ от 2026-06-26",
    }


def test_load_gate_active_restriction_with_swim_allowed():
    pstate = [{"Status": "active", "Contra_Load": "бег", "Confirmed_Date": "2026-06-26",
               "Condition": "Грыжа", "Allowed": "ходьба, плавание", "Provokers": "",
               "Review_Due": None, "Source": "МРТ"}]
    gate = _load_gate(pstate, {})
    assert gate["cap"] == 2


def test_load_gate_picks_most_recent_confirmed_date():
    pstate = [
        {"Status": "active", "Contra_Load": "бег", "Confirmed_Date": "2026-01-01", "Condition": "старое"},
        {"Status": "active", "Contra_Load": "прыжки", "Confirmed_Date": "2026-06-26", "Condition": "новое"},
    ]
    gate = _load_gate(pstate, {})
    assert gate["condition"] == "новое"


def test_load_gate_empty_patient_state_is_fail_safe_blocked():
    """A6 (ревью Opus 5, 2026-09-09): 0 строк = чтение не прошло, не «ограничений нет»."""
    gate = _load_gate([], {})
    assert gate["blocked"] is True
    assert gate["degraded"] is True
    assert gate["cap"] == 1


def test_load_gate_empty_patient_state_profile_confirms_hernia_allows_swim():
    profile = {"ОДА и неврология": "Межпозвоночная грыжа, плавание разрешено"}
    gate = _load_gate([], profile)
    assert gate["blocked"] is True
    assert gate["cap"] == 2
    assert "профиль подтверждает" in gate["condition"]


def test_load_gate_no_active_restriction_no_hernia_in_profile_not_blocked():
    pstate = [{"Status": "done", "Contra_Load": "", "Condition": "старое, снято"}]
    gate = _load_gate(pstate, {"ОДА и неврология": "ничего особенного"})
    assert gate == {"cap": 4, "blocked": False, "condition": None, "contra": None,
                     "allowed": "", "provokers": "", "review_due": None, "source": None}


def test_load_gate_patient_state_read_but_no_active_profile_confirms_hernia():
    pstate = [{"Status": "done", "Contra_Load": ""}]
    gate = _load_gate(pstate, {"ОДА и неврология": "грыжа L5/S1"})
    assert gate["blocked"] is True
    assert gate["source"] == "User_Profile (Patient_State без активных ограничений)"


def test_load_gate_hernia_in_remission_not_blocked():
    pstate = [{"Status": "done", "Contra_Load": ""}]
    gate = _load_gate(pstate, {"ОДА и неврология": "грыжа L5/S1, полное восстановление"})
    assert gate["blocked"] is False


# --- _alcohol_effective_g (2026-09-22, по запросу Влада: кефир — не "выпил") --

def test_alcohol_effective_g_trace_amount_is_zero():
    """0.3 г из стакана кефира — ровно кейс, из-за которого попросили порог."""
    assert _alcohol_effective_g(0.3) == 0


def test_alcohol_effective_g_at_threshold_is_zero():
    assert _alcohol_effective_g(_ALCOHOL_TRACE_THRESHOLD_G) == 0


def test_alcohol_effective_g_above_threshold_passes_through():
    assert _alcohol_effective_g(_ALCOHOL_TRACE_THRESHOLD_G + 0.1) == _ALCOHOL_TRACE_THRESHOLD_G + 0.1
    assert _alcohol_effective_g(14) == 14  # банка пива — должно учитываться полностью


def test_alcohol_effective_g_none_or_zero_is_zero():
    assert _alcohol_effective_g(None) == 0
    assert _alcohol_effective_g(0) == 0


# --- _parse_actions -----------------------------------------------------------

def test_parse_actions_extracts_json_envelope():
    txt = 'Текст рекомендации.\n<<<ACTIONS\n{"actions": [{"title": "Ходьба 30 мин"}]}\nACTIONS>>>'
    parsed = _parse_actions(txt)
    assert parsed["actions"] == [{"title": "Ходьба 30 мин"}]


def test_parse_actions_no_envelope_returns_none():
    assert _parse_actions("просто текст без блока") is None


def test_parse_actions_malformed_json_returns_none():
    assert _parse_actions("<<<ACTIONS {not valid json} ACTIONS>>>") is None


def test_parse_actions_missing_actions_key_returns_none():
    assert _parse_actions('<<<ACTIONS {"foo": 1} ACTIONS>>>') is None


# --- _parse_target_num ----------------------------------------------------

def test_parse_target_num_plain():
    assert _parse_target_num("50") == 50.0


def test_parse_target_num_comma_decimal():
    assert _parse_target_num("27,5") == 27.5


def test_parse_target_num_not_set_prefix_is_none():
    assert _parse_target_num("не установлено") is None


def test_parse_target_num_none_is_none():
    assert _parse_target_num(None) is None


# --- misc helpers -----------------------------------------------------------

def test_hhmm_wraps_past_midnight():
    assert _hhmm(1410) == "23:30"
    assert _hhmm(1440 + 30) == "00:30"
    assert _hhmm(-30) == "23:30"


def test_round4_rounds_to_four_decimals():
    assert _round4(0.123456) == 0.1235


def test_action_id_truncates_before_collapsing_whitespace():
    # порядок как в JS actionId(): slice(0,60) ДО схлопывания пробелов
    title = "а" * 58 + "   " + "б" * 10
    aid = _action_id("2026-09-13", title)
    assert aid.startswith("2026-09-13|")
    assert len(aid.split("|", 1)[1]) <= 60


def test_action_id_stable_for_same_issued_and_title():
    assert _action_id("2026-09-13", "Ходьба") == _action_id("2026-09-13", "Ходьба")


# --- endpoint / real-data smoke ---------------------------------------------

def test_dashboard_today_endpoint_shape():
    r = client.get("/dashboard/today", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("updated_at", "date", "now_local", "data_date", "decision", "windows",
                "streaks", "budget", "kcal_today", "protein_today", "meals_today",
                "plan", "longevity", "quiet"):
        assert key in body
    assert "gate" in body["decision"]
    assert isinstance(body["decision"]["gate"]["blocked"], bool)
    assert isinstance(body["windows"], list) and len(body["windows"]) == 3


def test_dashboard_today_wrong_token_forbidden():
    r = client.get("/dashboard/today", params={"token": "wrong"})
    assert r.status_code == 403


def test_dashboard_today_movement_and_swim_streaks_are_well_formed():
    """2026-09-23 (ходьба каждые 40 мин / плавание на неделе, разбор L5/S1):
    реальные данные — просто проверяем форму, если находка есть (не гоняемся
    за конкретным числом, которое меняется каждый день)."""
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_today_dashboard(cur)
    for s in result["streaks"]:
        if s["label"] in ("Вставал каждые 40 мин", "Плавание на неделе"):
            assert isinstance(s["count"], int) and s["count"] > 0
            assert s["unit"] in ("дней", "раз")


def test_dashboard_today_gate_blocked_on_real_data():
    """Инвариант проекта: активная грыжа L5/S1 в health.patient_state — гейт
    ОБЯЗАН быть blocked (тот же инвариант, что system_check._check_load_gate)."""
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_today_dashboard(cur)
    assert result["decision"]["gate"]["blocked"] is True
    assert result["decision"]["final_cap"] <= 2
