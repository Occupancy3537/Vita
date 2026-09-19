"""app/dashboard.py::get_weekly_nutrition — порт n8n «Получение данных питания
в кэш для Дашборда» / webhook weekly-nutrients (2026-09-20). health.day_sum/
nutrient_targets/meals/recommendations_log — реальная прод-схема (тот же
принцип, что test_dashboard_today.py) — юниты на чистые хелперы (JS-round,
plant-normализация с известным багом оригинала, диета-квалити хелперы), форма
и точные значения — сверка на реальных данных."""
from fastapi.testclient import TestClient

from app.dashboard import (
    _ahei_alcohol_pts,
    _ahei_lin,
    _js_round,
    _plant_norm,
    get_weekly_nutrition,
)
from app.main import app

client = TestClient(app)


# --- _js_round: JS Math.round (половина ВВЕРХ), не банковское ---------------

def test_js_round_half_rounds_up_not_to_even():
    """Реальный кейс, пойманный на живых данных 2026-09-20: round(12.5) даёт
    12 в Python (банковское), 13 в JS — здесь должно быть как в JS."""
    assert _js_round(12.5) == 13
    assert _js_round(0.5) == 1
    assert _js_round(2.5) == 3  # Python round(2.5) == 2 (к чётному) — не то, что нужно


def test_js_round_matches_python_off_half_boundary():
    assert _js_round(12.4) == 12
    assert _js_round(12.6) == 13


# --- _plant_norm: сохранённый баг оригинала (\w не матчит кириллицу в JS) ----

def test_plant_norm_adjective_prefix_not_actually_stripped():
    """Это ЗАДОКУМЕНТИРОВАННЫЙ баг оригинала, не мой: ADJ-регэксп на реальных
    инфлексированных формах не срабатывает (см. комментарий у _ADJ_RX) —
    "болгарский перец" не превращается в "перец", а превращается в "болгарск"
    (первое слово, суффикс "ий" срезан отдельным рабочим регэкспом)."""
    assert _plant_norm("болгарский перец") == "болгарски"


def test_plant_norm_single_word_strips_known_suffix():
    assert _plant_norm("яблоки") == "яблок"
    assert _plant_norm("морковь") == "морковь"  # "ь" не входит в список суффиксов — не срезается


def test_plant_norm_applies_synonym_on_full_raw_string():
    assert _plant_norm("Помидор") == "томат"


# --- diet-quality helpers -----------------------------------------------------

def test_ahei_lin_clamps_to_0_10_range():
    assert _ahei_lin(-5, 0, 5) == 0
    assert _ahei_lin(100, 0, 5) == 10
    assert _ahei_lin(2.5, 0, 5) == 5


def test_ahei_alcohol_pts_zero_drinks_male():
    assert _ahei_alcohol_pts(0, male=True) == 2.5


def test_ahei_alcohol_pts_optimal_range_male():
    assert _ahei_alcohol_pts(1.0, male=True) == 10


def test_ahei_alcohol_pts_excess_is_zero():
    assert _ahei_alcohol_pts(10, male=True) == 0


# --- endpoint / real-data smoke ---------------------------------------------

def test_dashboard_weekly_nutrition_endpoint_shape():
    r = client.get("/dashboard/weekly-nutrition", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("period", "days", "scores", "bullets", "heatmap", "diet_quality",
                "normal", "sources", "computed_at", "nutrition_loops"):
        assert key in body
    assert len(body["days"]) <= 7
    assert "ahei" in body["diet_quality"]
    assert "nova" in body["diet_quality"]
    assert "plants" in body["diet_quality"]


def test_dashboard_weekly_nutrition_wrong_token_forbidden():
    r = client.get("/dashboard/weekly-nutrition", params={"token": "wrong"})
    assert r.status_code == 403


def test_get_weekly_nutrition_period_matches_days_bounds():
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_weekly_nutrition(cur)
    if result["days"]:
        assert result["period"]["from"] == result["days"][0]
        assert result["period"]["to"] == result["days"][-1]


def test_get_weekly_nutrition_sources_only_for_heatmap_or_limit_nutrients():
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_weekly_nutrition(cur)
    heatmap_labels = {m["label"] for m in result["heatmap"]}
    bullet_labels = {b["label"] for b in result["bullets"]}
    assert set(result["sources"].keys()) <= (heatmap_labels | bullet_labels)
