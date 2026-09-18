"""app/dashboard.py — live «сегодня»-метрики (steps/kcal/protein), замена
n8n-кэша health-dashboard (см. STATE.md 2026-09-16, docstring app/dashboard.py).

health.* — общая прод-схема без card_test-зеркала (та же ситуация, что и у
commit.py). Здесь это не проблема: функция только читает, ничего не пишет — юнит-
тесты мокают курсор (детерминированно, без прод-данных), а тест эндпоинта бьёт по
реальной БД и проверяет только форму ответа, не конкретные цифры (они меняются
каждый день по определению фичи)."""
from datetime import date, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.dashboard import _baseline_for, _judge, _r_smart, get_today_live_metrics
from app.main import app

client = TestClient(app)


class _FakeDate:
    """Заглушка вместо datetime.date — только .isoformat(), как в проде."""
    def __init__(self, s):
        self._s = s

    def isoformat(self):
        return self._s


def _mock_cur(meal_rows, steps_row):
    cur = MagicMock()
    cur.fetchall.return_value = meal_rows
    cur.fetchone.return_value = steps_row
    return cur


def test_get_today_live_metrics_normal_day():
    # meals."Calories"/"Proteins" — TEXT-колонки, приходят строками, иногда с
    # запятой вместо точки (наступили на это в pg_sheets_diff_check).
    meal_rows = [("2247", "132,2"), ("100", "5")]
    cur = _mock_cur(meal_rows, (11427, 42, _FakeDate("2026-09-16"), "irrelevant"))
    out = get_today_live_metrics(cur)
    assert out["kcal_today_live"] == 2347
    assert out["protein_today_live"] == 137
    assert out["steps_today_live"] == 11427
    assert out["stress_today_live"] == 42
    assert out["meals_count_today"] == 2
    assert out["steps_source_date"] == "2026-09-16"


def test_get_today_live_metrics_no_stress_yet():
    # Стресс приходит из Garmin — свежее внутридневное значение; до синка его нет.
    # Не должен ломать метрику: stress_today_live честно null, а не 0.
    meal_rows = [("600", "30")]
    cur = _mock_cur(meal_rows, (2000, None, _FakeDate("2026-09-18"), "irrelevant"))
    out = get_today_live_metrics(cur)
    assert out["steps_today_live"] == 2000
    assert out["stress_today_live"] is None


def test_get_today_live_metrics_no_meals_yet():
    cur = _mock_cur([], (500, 30, _FakeDate("2026-09-16"), "irrelevant"))
    out = get_today_live_metrics(cur)
    assert out["kcal_today_live"] == 0
    assert out["protein_today_live"] == 0
    assert out["meals_count_today"] == 0


def test_get_today_live_metrics_no_steps_row_yet():
    """push_live_steps.py ещё не отработал сегодня (например, самое начало
    суток) — steps_today_live честно null, а не 0 и не вчерашнее значение."""
    cur = _mock_cur([("695", "55")], None)
    out = get_today_live_metrics(cur)
    assert out["steps_today_live"] is None
    assert out["stress_today_live"] is None
    assert out["steps_source_date"] is None
    assert out["kcal_today_live"] == 695


def test_get_today_live_metrics_skips_unparseable_values():
    """Пустая строка/мусор в TEXT-колонке — пропускаем, не роняем весь расчёт
    (тот же принцип устойчивости, что в get_meals_today)."""
    meal_rows = [("120", ""), ("не число", "10"), ("80", "3")]
    cur = _mock_cur(meal_rows, None)
    out = get_today_live_metrics(cur)
    assert out["kcal_today_live"] == 200  # 120 + 80, "не число" пропущено
    assert out["protein_today_live"] == 13  # 10 + 3, "" пропущено


def test_dashboard_today_live_endpoint_shape():
    # nginx проксирует /card/ без проверок (см. main.py) — токен теперь
    # проверяет сам эндпоинт, .env.test задаёт DASHBOARD_TOKEN отдельно от прода.
    r = client.get("/dashboard/today-live", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in (
        "steps_today_live", "kcal_today_live", "protein_today_live",
        "meals_count_today", "steps_source_date", "computed_at",
    ):
        assert key in body
    assert isinstance(body["meals_count_today"], int)
    assert body["computed_at"]  # непустая ISO-строка на каждый вызов


def test_dashboard_today_live_wrong_token_forbidden():
    r = client.get("/dashboard/today-live", params={"token": "wrong"})
    assert r.status_code == 403


def test_dashboard_today_live_missing_token_forbidden():
    r = client.get("/dashboard/today-live")
    assert r.status_code == 403


# --- /dashboard/health — порт Build Health JSON (математика, не форма ответа) ---

def test_judge_neutral_direction_always_neutral():
    assert _judge("neutral", 999, 1) == "neutral"


def test_judge_below_noise_threshold_is_neutral():
    assert _judge("higher_better", 2, 6) == "neutral"


def test_judge_higher_better():
    assert _judge("higher_better", 10, 6) == "good"
    assert _judge("higher_better", -10, 6) == "bad"


def test_judge_lower_better():
    assert _judge("lower_better", -10, 3) == "good"
    assert _judge("lower_better", 10, 3) == "bad"


def test_r_smart_rounds_small_values_to_one_decimal_large_to_int():
    assert _r_smart(47.36) == 47.4  # ВСР — десятые важны
    assert _r_smart(11427.2) == 11427  # шаги — десятые не нужны
    assert _r_smart(None) is None


def test_baseline_for_uses_only_days_strictly_before_current():
    """30-дневное окно — [текущий_день - 30, текущий_день), сам текущий день
    в базу не входит (иначе метрика сравнивала бы себя с собой)."""
    today = date(2026, 9, 16)
    rows = [(today - timedelta(days=i), {"x": 40.0}) for i in range(15, 0, -1)]
    rows.append((today, {"x": 100.0}))  # текущий день — заведомо выброс
    base = _baseline_for(rows, "x", len(rows) - 1, 30, 10)
    assert base is not None
    assert base["mean"] == 40.0  # если бы 100 попало в базу, среднее бы уехало
    assert base["n"] == 15


def test_baseline_for_insufficient_points_returns_none():
    today = date(2026, 9, 16)
    rows = [(today - timedelta(days=1), {"x": 40.0}), (today, {"x": 50.0})]
    assert _baseline_for(rows, "x", 1, 30, 10) is None


def test_dashboard_health_endpoint_shape():
    """Бьёт по реальной health.daily_trends — проверяет форму, не цифры (эти
    меняются каждый день, а health.anomaly_log вообще пока пуст — заполнится
    первым ночным прогоном Anomaly_Detector после переноса, см. STATE.md)."""
    r = client.get("/dashboard/health", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("updated_at", "today", "metrics", "days_14", "trends",
                "investigations", "medical_notes_recent", "anomalies",
                "correlations", "experiments", "experiments_note"):
        assert key in body
    assert isinstance(body["metrics"], list) and len(body["metrics"]) > 0
    assert isinstance(body["days_14"], list)
    keys = {m["key"] for m in body["metrics"]}
    assert "hrv" in keys and "steps_today_live" in keys
    assert body["anomalies"]["status"] in ("flagged", "clean", "not_run")
    assert body["correlations"] == {"computed": None, "disabled": True, "priority": [], "discovery": []}
    assert body["experiments"] == []


def test_dashboard_health_wrong_token_forbidden():
    r = client.get("/dashboard/health", params={"token": "wrong"})
    assert r.status_code == 403
