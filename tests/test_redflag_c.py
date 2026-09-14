"""П5 §5 — слой C: метрические правила, personal baseline (self-vs-self), не
популяционный. Проверено на синтетических, но физиологически правдоподобных
рядах (реальные пороги из спеки: rhr+7, hrv*0.7, sleep<360/330)."""
from datetime import timedelta

from app.db import get_conn, schema
from app.redflag_c import check_infectious_pattern, check_recovery_collapse, run_layer_c


def _insert_daily(cur, metric_key: str, values_by_days_ago: dict):
    for days_ago, val in values_by_days_ago.items():
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
            f"VALUES (%s, now() - (%s || ' days')::interval, '{{}}', 'confirmed', %s, %s)",
            (f"f_c_{metric_key}_{days_ago}", days_ago, metric_key, val),
        )


def test_infectious_pattern_triggers_on_matching_two_days():
    with get_conn() as conn, conn.cursor() as cur:
        # baseline 7 дней RHR ~50, последние 2 дня — 60+ (>= baseline+7)
        _insert_daily(cur, "rhr", {8: 50, 7: 49, 6: 51, 5: 50, 4: 50, 3: 49, 2: 50, 1: 60, 0: 61})
        _insert_daily(cur, "stress", {1: 50, 0: 55})
        _insert_daily(cur, "sleep_min", {1: 300, 0: 280})
        conn.commit()
        result = check_infectious_pattern(cur)
    assert result is not None
    assert result["rule"] == "rf-c-01" and result["level"] == "L1" and result["category"] == "systemic_warning"


def test_infectious_pattern_not_triggered_when_stress_normal():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "rhr", {8: 50, 7: 49, 6: 51, 5: 50, 4: 50, 3: 49, 2: 50, 1: 60, 0: 61})
        _insert_daily(cur, "stress", {1: 20, 0: 25})  # низкий стресс — правило не должно сработать
        _insert_daily(cur, "sleep_min", {1: 300, 0: 280})
        conn.commit()
        result = check_infectious_pattern(cur)
    assert result is None


def test_infectious_pattern_needs_two_full_days_not_one():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "rhr", {8: 50, 7: 49, 6: 51, 5: 50, 4: 50, 3: 49, 1: 50, 0: 61})  # только 1 день выше
        _insert_daily(cur, "stress", {1: 50, 0: 55})
        _insert_daily(cur, "sleep_min", {1: 300, 0: 280})
        conn.commit()
        result = check_infectious_pattern(cur)
    assert result is None


def test_recovery_collapse_triggers_on_matching_three_days():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "hrv", {9: 50, 8: 48, 7: 52, 6: 49, 5: 51, 4: 50, 3: 49, 2: 30, 1: 28, 0: 29})
        _insert_daily(cur, "rhr", {9: 48, 8: 47, 7: 49, 6: 48, 5: 48, 4: 47, 3: 48, 2: 55, 1: 56, 0: 54})
        _insert_daily(cur, "sleep_min", {2: 300, 1: 290, 0: 310})
        conn.commit()
        result = check_recovery_collapse(cur)
    assert result is not None and result["rule"] == "rf-c-02"


def test_recovery_collapse_not_triggered_with_normal_hrv():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "hrv", {9: 50, 8: 48, 7: 52, 6: 49, 5: 51, 4: 50, 3: 49, 2: 48, 1: 47, 0: 49})  # HRV в норме
        _insert_daily(cur, "rhr", {9: 48, 8: 47, 7: 49, 6: 48, 5: 48, 4: 47, 3: 48, 2: 55, 1: 56, 0: 54})
        _insert_daily(cur, "sleep_min", {2: 300, 1: 290, 0: 310})
        conn.commit()
        result = check_recovery_collapse(cur)
    assert result is None


def test_insufficient_history_returns_none_not_crash():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "rhr", {1: 60, 0: 61})  # мало дней для baseline
        conn.commit()
        assert check_infectious_pattern(cur) is None
        assert check_recovery_collapse(cur) is None


def test_run_layer_c_aggregates_both_rules():
    with get_conn() as conn, conn.cursor() as cur:
        _insert_daily(cur, "rhr", {8: 50, 7: 49, 6: 51, 5: 50, 4: 50, 3: 49, 2: 50, 1: 60, 0: 61})
        _insert_daily(cur, "stress", {1: 50, 0: 55})
        _insert_daily(cur, "sleep_min", {1: 300, 0: 280})
        conn.commit()
        results = run_layer_c(cur)
    assert any(r["rule"] == "rf-c-01" for r in results)
