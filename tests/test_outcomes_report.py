"""app/outcomes_report.py — мета-отчёт «что на мне работает» (тикет «хвост»,
2026-09-26, Часть 3). card.recommendation/expectation/recommendation_verdict/
publication — обычные card.* таблицы, изолированные схемой card_test в тестах
(та же ситуация, что test_recommendations.py/test_weekly_advisor.py) — отдельная
_isolate_real_schema_writes не нужна, сюда ничего боевого не пишем."""
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app import outcomes_report as outr
from app.db import get_conn, schema
from app.main import app

client = TestClient(app)


def _seed_recommendation(cur, rec_id, title="Тестовая рекомендация", status="active",
                          stop_reason=None, publication_id=None):
    cur.execute(
        f"INSERT INTO {schema()}.recommendation "
        "(id, ts_event, provenance, title, status, stop_reason, publication_id) "
        "VALUES (%s, now(), '{}', %s, %s, %s, %s)",
        (rec_id, title, status, stop_reason, publication_id),
    )


def _seed_expectation(cur, rec_id, metric_key="test_metric", metric_label="Тестовая метрика",
                       unit="ед.", window_days=7, cycle=1):
    cur.execute(
        f"INSERT INTO {schema()}.expectation "
        "(id, rec_id, cycle, type, metric_key, metric_label, unit, window_days, role) "
        "VALUES (%s, %s, %s, 'delta_abs', %s, %s, %s, %s, 'primary')",
        (f"ex_{rec_id}", rec_id, cycle, metric_key, metric_label, unit, window_days),
    )


def _seed_publication(cur, pub_id, title="Тестовая публикация", url="https://example.org/paper"):
    cur.execute(
        f"INSERT INTO {schema()}.publication "
        "(id, ts_event, provenance, source, title, url) "
        "VALUES (%s, now(), '{}', 'test', %s, %s)",
        (pub_id, title, url),
    )


def _seed_verdict(cur, rec_id, verdict, ts_computed, cycle=1, status="current",
                   metric_key="test_metric", confounded=None, rule_trace=None):
    cur.execute(
        f"INSERT INTO {schema()}.recommendation_verdict "
        "(id, rec_id, cycle, engine_version, verdict, ts_computed, status, metric_key, confounded, rule_trace) "
        "VALUES (%s, %s, %s, 'v1', %s, %s, %s, %s, %s, %s)",
        (f"rv_{rec_id}_{cycle}", rec_id, cycle, verdict, ts_computed, status, metric_key, confounded, rule_trace),
    )


# --- _quarter_bounds / _prev_quarter_bounds ---

def test_quarter_bounds_middle_of_quarter():
    assert outr._quarter_bounds(date(2026, 8, 15)) == (date(2026, 7, 1), date(2026, 10, 1))


def test_quarter_bounds_first_month_of_quarter():
    assert outr._quarter_bounds(date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 4, 1))


def test_quarter_bounds_q4_year_rollover():
    assert outr._quarter_bounds(date(2026, 11, 20)) == (date(2026, 10, 1), date(2027, 1, 1))


def test_prev_quarter_bounds_normal():
    assert outr._prev_quarter_bounds(date(2026, 8, 15)) == (date(2026, 4, 1), date(2026, 7, 1))


def test_prev_quarter_bounds_across_year_boundary():
    # текущий квартал — Q1 2026 (янв-мар), прошлый — Q4 2025 (окт-дек)
    assert outr._prev_quarter_bounds(date(2026, 2, 10)) == (date(2025, 10, 1), date(2026, 1, 1))


# --- _working_share ---

def test_working_share_empty_is_none():
    assert outr._working_share([]) is None


def test_working_share_mixed_verdicts():
    rows = [{"verdict": "effective"}, {"verdict": "partial"}, {"verdict": "no_effect"}, {"verdict": "data_gap"}]
    assert outr._working_share(rows) == 50  # 2 из 4


def test_working_share_all_working():
    rows = [{"verdict": "effective"}, {"verdict": "partial"}]
    assert outr._working_share(rows) == 100


# --- get_outcomes_detail ---

def test_get_outcomes_detail_joins_expectation_and_publication():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_publication(cur, "pub_outr_1")
        _seed_recommendation(cur, "rec_outr_1", title="Отбой в 23:00", publication_id="pub_outr_1")
        _seed_expectation(cur, "rec_outr_1", metric_key="test_hrv", metric_label="ВСР", unit="мс")
        _seed_verdict(cur, "rec_outr_1", "effective", datetime(2026, 9, 15, tzinfo=timezone.utc))
        conn.commit()
        rows = outr.get_outcomes_detail(cur)
    row = next(r for r in rows if r["rec_id"] == "rec_outr_1")
    assert row["verdict"] == "effective"
    assert row["title"] == "Отбой в 23:00"
    assert row["metric_label"] == "ВСР"
    assert row["unit"] == "мс"
    assert row["publication_title"] == "Тестовая публикация"
    assert row["publication_url"] == "https://example.org/paper"


def test_get_outcomes_detail_excludes_superseded():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rec_outr_2")
        _seed_verdict(cur, "rec_outr_2", "no_effect", datetime(2026, 9, 1, tzinfo=timezone.utc),
                      cycle=1, status="superseded")
        _seed_verdict(cur, "rec_outr_2", "effective", datetime(2026, 9, 10, tzinfo=timezone.utc),
                      cycle=2, status="current")
        conn.commit()
        rows = outr.get_outcomes_detail(cur)
    matching = [r for r in rows if r["rec_id"] == "rec_outr_2"]
    assert len(matching) == 1
    assert matching[0]["verdict"] == "effective"


def test_get_outcomes_detail_without_expectation_or_publication_still_returns_row():
    """rec без publication_id и без primary-expectation (например, unmeasurable) —
    LEFT JOIN, не должен отфильтровать строку вердикта."""
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rec_outr_3")
        _seed_verdict(cur, "rec_outr_3", "data_gap", datetime(2026, 9, 5, tzinfo=timezone.utc))
        conn.commit()
        rows = outr.get_outcomes_detail(cur)
    row = next(r for r in rows if r["rec_id"] == "rec_outr_3")
    assert row["verdict"] == "data_gap"
    assert row["metric_label"] is None
    assert row["publication_title"] is None


# --- quarterly_summary ---

def test_quarterly_summary_counts_and_working_share():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rec_outr_q1")
        _seed_verdict(cur, "rec_outr_q1", "effective", datetime(2026, 8, 5, tzinfo=timezone.utc))
        _seed_recommendation(cur, "rec_outr_q2")
        _seed_verdict(cur, "rec_outr_q2", "no_effect", datetime(2026, 8, 10, tzinfo=timezone.utc))
        conn.commit()
        summary = outr.quarterly_summary(cur, today=date(2026, 8, 20))
    assert summary["total"] >= 2
    assert summary["quarter"] == {"from": "2026-07-01", "to": "2026-10-01"}
    assert summary["counts"]["effective"] >= 1
    assert summary["counts"]["no_effect"] >= 1
    assert summary["working_share_pct"] is not None


def test_quarterly_summary_trend_against_previous_quarter():
    with get_conn() as conn, conn.cursor() as cur:
        # прошлый квартал (Q2 2026, апр-июн) — 0% рабочих
        _seed_recommendation(cur, "rec_outr_trend_prev")
        _seed_verdict(cur, "rec_outr_trend_prev", "no_effect", datetime(2026, 5, 1, tzinfo=timezone.utc))
        # текущий квартал (Q3 2026, июл-сен) — 100% рабочих
        _seed_recommendation(cur, "rec_outr_trend_cur")
        _seed_verdict(cur, "rec_outr_trend_cur", "effective", datetime(2026, 8, 1, tzinfo=timezone.utc))
        conn.commit()
        summary = outr.quarterly_summary(cur, today=date(2026, 8, 20))
    assert summary["working_share_trend_pts"] is not None
    assert summary["working_share_trend_pts"] > 0


def test_quarterly_summary_closed_list_includes_stop_reason():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rec_outr_closed", title="Магний вечером", status="closed",
                              stop_reason="не сработало за 2 цикла")
        _seed_verdict(cur, "rec_outr_closed", "no_effect", datetime(2026, 8, 12, tzinfo=timezone.utc))
        conn.commit()
        summary = outr.quarterly_summary(cur, today=date(2026, 8, 20))
    closed_titles = [c["title"] for c in summary["closed"]]
    assert "Магний вечером" in closed_titles
    closed_row = next(c for c in summary["closed"] if c["title"] == "Магний вечером")
    assert closed_row["stop_reason"] == "не сработало за 2 цикла"


def test_quarterly_summary_empty_quarter_has_total_zero_and_no_trend():
    with get_conn() as conn, conn.cursor() as cur:
        summary = outr.quarterly_summary(cur, today=date(2019, 3, 1))
    assert summary["total"] == 0
    assert summary["working_share_pct"] is None
    assert summary["working_share_trend_pts"] is None
    assert summary["closed"] == []


# --- build_digest_line ---

def test_build_digest_line_empty_is_none():
    assert outr.build_digest_line({"total": 0, "working_share_pct": None}) is None


def test_build_digest_line_matches_exact_wording():
    line = outr.build_digest_line({"total": 3, "working_share_pct": 67})
    assert line == "Мета-отчёт обновлён: 3 вердиктов, доля работающего — 67%."


# --- /outcomes/detail и /outcomes/quarterly эндпоинты ---

def test_outcomes_detail_endpoint_requires_token():
    r = client.get("/outcomes/detail")
    assert r.status_code == 403


def test_outcomes_detail_endpoint_wrong_token_forbidden():
    r = client.get("/outcomes/detail", params={"token": "wrong"})
    assert r.status_code == 403


def test_outcomes_detail_endpoint_shape():
    r = client.get("/outcomes/detail", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_outcomes_quarterly_endpoint_requires_token():
    r = client.get("/outcomes/quarterly")
    assert r.status_code == 403


def test_outcomes_quarterly_endpoint_shape():
    r = client.get("/outcomes/quarterly", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("quarter", "total", "counts", "working_share_pct", "working_share_trend_pts", "closed"):
        assert key in body
