"""app/dashboard.py — live «сегодня»-метрики (steps/kcal/protein), замена
n8n-кэша health-dashboard (см. STATE.md 2026-09-16, docstring app/dashboard.py).

health.* — общая прод-схема без card_test-зеркала (та же ситуация, что и у
commit.py). Здесь это не проблема: функция только читает, ничего не пишет — юнит-
тесты мокают курсор (детерминированно, без прод-данных), а тест эндпоинта бьёт по
реальной БД и проверяет только форму ответа, не конкретные цифры (они меняются
каждый день по определению фичи)."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.dashboard import _baseline_for, _judge, _r_smart, get_today_live_metrics
from app.db import get_conn, schema
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


# ─────── _build_reasons / _budget_for_day / _baseline_metrics_for_index ───────
# Vita v2, этап 1 (2026-09-28) — извлечены из get_today_dashboard/
# get_health_dashboard в чистые функции: и живой путь, и калибровка ring.ahead
# на истории (app/vita_calibration.py) вызывают ИХ ЖЕ, не второй набор порогов.

from app.dashboard import _budget_for_day, _baseline_metrics_for_index, _build_reasons


def test_build_reasons_body_battery_and_hrv_are_recovery_segment():
    reasons = _build_reasons(bb=80, hrv_delta=2.0, acwr=None, acwr_status_g=None,
                              acwr_source=None, load_high=False)
    labels = {r["label"]: r["segment"] for r in reasons}
    assert labels["Body Battery"] == "recovery"
    assert labels["ВСР к базе"] == "recovery"


def test_build_reasons_acwr_is_move_segment():
    reasons = _build_reasons(bb=None, hrv_delta=None, acwr=1.6, acwr_status_g="HIGH",
                              acwr_source="garmin", load_high=True)
    assert reasons[0]["segment"] == "move"
    assert reasons[0]["judgment"] == "bad"


def test_build_reasons_empty_when_no_inputs():
    assert _build_reasons(None, None, None, None, None, False) == []


def test_budget_for_day_over_clinical_limit_marks_status_over():
    meals = [{"Date": "2026-09-20T08:00", "Натрий": "3000"}]
    targets = [{"Нутриент": "Натрий", "Колонка_в_Meals": "Натрий", "Категория": "Риск избытка",
                "Верхний_предел_UL": "2300", "Единица": "мг"}]
    budget = _budget_for_day(meals, targets, "2026-09-20")
    assert len(budget) == 1
    assert budget[0]["status"] == "over" and budget[0]["kind"] == "limit"


def test_budget_for_day_only_counts_matching_calendar_day():
    meals = [{"Date": "2026-09-19T08:00", "Натрий": "3000"}, {"Date": "2026-09-20T08:00", "Натрий": "500"}]
    targets = [{"Нутриент": "Натрий", "Колонка_в_Meals": "Натрий", "Категория": "Риск избытка",
                "Верхний_предел_UL": "2300", "Единица": "мг"}]
    budget = _budget_for_day(meals, targets, "2026-09-20")
    assert budget[0]["consumed"] == 500.0  # не 3500 — 19.09 не в этом дне


def test_baseline_metrics_for_index_works_for_a_past_day_not_only_last():
    today = date(2026, 9, 20)
    rows = [(today - timedelta(days=i), {"ВСР_ночная": 50.0}) for i in range(20, 0, -1)]
    rows.append((today, {"ВСР_ночная": 65.0}))  # последний день — выброс, не должен попасть в базу
    metrics_at_last = _baseline_metrics_for_index(rows, len(rows) - 1)
    metrics_mid = _baseline_metrics_for_index(rows, 10)
    assert metrics_at_last["hrv"]["value"] == 65.0
    assert metrics_at_last["hrv"]["baseline"] == 50.0
    assert metrics_mid["hrv"]["value"] == 50.0  # день из середины истории тоже считается, не только последний


def test_dashboard_health_endpoint_shape():
    """Бьёт по реальной health.daily_trends — проверяет форму, не цифры (эти
    меняются каждый день, а health.anomaly_log вообще пока пуст — заполнится
    первым ночным прогоном Anomaly_Detector после переноса, см. STATE.md)."""
    r = client.get("/dashboard/health", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("updated_at", "today", "metrics", "days_14", "trends",
                "investigations", "medical_notes_recent", "anomalies",
                "action_loops", "pending_anomalies"):
        assert key in body
    # «Стоп-кровь каналов» (2026-09-26, часть 2.2): correlations/experiments —
    # заглушки отключённого движка, мёртвые поля без потребителя — убраны из
    # ответа совсем, не проверяем их отсутствием ключа И значением сразу.
    assert "correlations" not in body
    assert "experiments" not in body
    assert "experiments_note" not in body
    assert isinstance(body["metrics"], list) and len(body["metrics"]) > 0
    assert isinstance(body["days_14"], list)
    # D9 (аудит логики, 2026-09-23): action_loops был мёртвым полем — фронт его
    # ждал (блок "Прижилось"), бэкенд никогда не отдавал. Список, не падает.
    assert isinstance(body["action_loops"], list)
    keys = {m["key"] for m in body["metrics"]}
    assert "hrv" in keys and "steps_today_live" in keys
    assert body["anomalies"]["status"] in ("flagged", "clean", "not_run")


def test_dashboard_health_surfaces_action_loops():
    """D9: /dashboard/health реально прокидывает get_loops() — не только
    отдаёт пустой список по умолчанию, а видит то, что там появилось."""
    started = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        for i in range(1, 8):
            cur.execute(
                f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
                f"VALUES (%s, %s, %s, 'confirmed', %s, %s)",
                (f"f_seed_dloop_{i}", started - timedelta(days=i), '{"origin":"device"}', "test_dashboard_loop", 40),
            )
        for i in range(0, 7):
            cur.execute(
                f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
                f"VALUES (%s, %s, %s, 'confirmed', %s, %s)",
                (f"f_seed_dloop_after_{i}", started + timedelta(days=i), '{"origin":"device"}', "test_dashboard_loop", 46),
            )
        conn.commit()
    sync_r = client.post("/recommendations/sync", json={
        "title": "Тестовый цикл дашборда", "rationale": "проверка D9", "source_ref": "rec_test_dashboard_loop",
        "started_ts": started.isoformat(), "metric_key": "test_dashboard_loop",
        "direction": "up", "magnitude": 6, "window_days": 7, "lag_days": 0,
    })
    client.post(f"/recommendations/{sync_r.json()['id']}/evaluate")

    body = client.get("/dashboard/health", params={"token": "test-dashboard-token-not-prod"}).json()
    assert any(l.get("metric") == "test_dashboard_loop" for l in body["action_loops"])


def test_dashboard_health_surfaces_pending_anomaly_dispositions():
    """«Пересборка вычитанием» (2026-09-26, Часть 1.1): лента решений на
    главном экране читает pending-диспозиции через /dashboard/health — тот
    же читатель card.anomaly_disposition, что и досье доктора
    (recent_dispositions), просто про pending, а не про уже решённое."""
    from app import anomaly_disposition as ad

    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "test_dashboard_pending_metric", "Тестовая метрика",
                                   "2026-09-20", "strong")
        conn.commit()
    body = client.get("/dashboard/health", params={"token": "test-dashboard-token-not-prod"}).json()
    assert isinstance(body["pending_anomalies"], list)
    assert any(p.get("metric_key") == "test_dashboard_pending_metric" for p in body["pending_anomalies"])


def test_dashboard_health_wrong_token_forbidden():
    r = client.get("/dashboard/health", params={"token": "wrong"})
    assert r.status_code == 403
