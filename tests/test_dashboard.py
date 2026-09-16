"""app/dashboard.py — live «сегодня»-метрики (steps/kcal/protein), замена
n8n-кэша health-dashboard (см. STATE.md 2026-09-16, docstring app/dashboard.py).

health.* — общая прод-схема без card_test-зеркала (та же ситуация, что и у
commit.py). Здесь это не проблема: функция только читает, ничего не пишет — юнит-
тесты мокают курсор (детерминированно, без прод-данных), а тест эндпоинта бьёт по
реальной БД и проверяет только форму ответа, не конкретные цифры (они меняются
каждый день по определению фичи)."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.dashboard import get_today_live_metrics
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
    cur = _mock_cur(meal_rows, (11427, _FakeDate("2026-09-16"), "irrelevant"))
    out = get_today_live_metrics(cur)
    assert out["kcal_today_live"] == 2347
    assert out["protein_today_live"] == 137
    assert out["steps_today_live"] == 11427
    assert out["meals_count_today"] == 2
    assert out["steps_source_date"] == "2026-09-16"


def test_get_today_live_metrics_no_meals_yet():
    cur = _mock_cur([], (500, _FakeDate("2026-09-16"), "irrelevant"))
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
