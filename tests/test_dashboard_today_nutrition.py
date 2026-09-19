"""app/dashboard.py::get_today_nutrition — порт n8n `Dashboard Cached` /
webhook today-nutrition (2026-09-20). health.meals/nutrition_profile/
anomaly_log/daily_trends — реальная прод-схема (тот же принцип, что
test_dashboard_today.py) — юниты на чистые хелперы, форма/значения на
реальных данных."""
from fastapi.testclient import TestClient

from app.dashboard import _js_num, _or0, get_today_nutrition
from app.main import app

client = TestClient(app)


# --- _js_num: как JS Number(v) себя ведёт на "" / None / запятой ------------

def test_js_num_empty_and_none_are_zero():
    assert _js_num("") == 0.0
    assert _js_num(None) == 0.0


def test_js_num_parses_dot_decimal():
    assert _js_num("24.3") == 24.3


def test_js_num_comma_decimal_is_zero_not_parsed():
    """В отличие от _num()/toNum() в оригинале — Number("24,3") это NaN, а
    NaN || 0 это 0. Здесь так же, сознательно, не тише оригинала."""
    assert _js_num("24,3") == 0.0


# --- _or0: как JS `v || 0` ----------------------------------------------------

def test_or0_falsy_becomes_zero():
    assert _or0(None) == 0
    assert _or0("") == 0


def test_or0_truthy_string_kept_as_is():
    assert _or0("2400") == "2400"
    assert _or0("0") == "0"  # непустая строка "0" — truthy в JS, остаётся строкой


# --- endpoint / real-data smoke ---------------------------------------------

def test_dashboard_today_nutrition_endpoint_shape():
    r = client.get("/dashboard/today-nutrition", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("date", "user", "summary", "meals_count", "meals", "anomalies"):
        assert key in body
    for macro in ("proteins", "fats", "carbs"):
        assert macro in body["summary"]["macros"]
        for k in ("consumed", "target", "remaining"):
            assert k in body["summary"]["macros"][macro]
    assert "count" in body["anomalies"] and "raw" in body["anomalies"]


def test_dashboard_today_nutrition_wrong_token_forbidden():
    r = client.get("/dashboard/today-nutrition", params={"token": "wrong"})
    assert r.status_code == 403


def test_get_today_nutrition_meals_count_matches_meals_list():
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_today_nutrition(cur)
    assert result["meals_count"] == len(result["meals"])
    # meals отсортированы по времени по возрастанию
    times = [m["t"] for m in result["meals"]]
    assert times == sorted(times)


def test_get_today_nutrition_anomaly_history_length_capped_at_7():
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_today_nutrition(cur)
    for a in result["anomalies"]["raw"]:
        assert len(a["history"]) <= 7
