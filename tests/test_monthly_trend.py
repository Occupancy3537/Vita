"""app/monthly_trend.py — порт n8n Monthly_Trend_Wellness (2026-09-21,
последний пункт группы 1, 16 нод). Split&Tag/Aggregate Month/Compute Trend
были дублированы дважды в оригинале под разными именами узлов — здесь один
параметризованный набор функций, тесты покрывают оба домена через одни и
те же функции."""
from datetime import date, datetime

import pytest

from app import monthly_trend as mt
from app.db import get_conn


def test_to_number_handles_comma_and_percent():
    assert mt._to_number("24,3") == 24.3
    assert mt._to_number("85%") == 85
    assert mt._to_number("") is None
    assert mt._to_number(None) is None


def test_define_month_windows_on_first_of_month():
    now = datetime(2026, 9, 1, 10, 0)
    w = mt.define_month_windows(now)
    assert w == {"current_month": "2026-08", "previous_month": "2026-07"}


def test_define_month_windows_matches_calendar_minus_n_months_regardless_of_day():
    # оригинал (Luxon .minus({months: N})) не зависит от дня месяца, только
    # год-месяц имеет значение — порт должен давать тот же результат в любой день
    now = datetime(2026, 9, 21, 15, 30)
    w = mt.define_month_windows(now)
    assert w == {"current_month": "2026-08", "previous_month": "2026-07"}


def test_define_month_windows_handles_january():
    now = datetime(2026, 1, 15)
    w = mt.define_month_windows(now)
    assert w == {"current_month": "2025-12", "previous_month": "2025-11"}


def test_split_and_tag_current_and_previous():
    windows = {"current_month": "2026-09", "previous_month": "2026-08"}
    rows = [
        {"Date": "2026-09-05", "Calories": "2000"},
        {"Date": "2026-08-20", "Calories": "1900"},
        {"Date": "2026-07-01", "Calories": "1800"},  # ни тот, ни другой месяц
    ]
    tagged = mt.split_and_tag(rows, "Date", "nutrition", windows)
    assert len(tagged) == 2
    assert tagged[0]["__period"] == "current" and tagged[0]["domain"] == "nutrition"
    assert tagged[1]["__period"] == "previous"


def test_aggregate_month_total_and_average():
    rows = [
        {"Date": "2026-09-01", "__period": "current", "domain": "nutrition", "Calories": "2000", "Proteins": "100"},
        {"Date": "2026-09-02", "__period": "current", "domain": "nutrition", "Calories": "2200", "Proteins": "110"},
    ]
    result = mt.aggregate_month(rows, "2026-09")
    assert result is not None
    total, avg = result
    assert total["Calories"] == 4200.0
    assert avg["Calories"] == 2100.0
    assert total["Способ подсчета"] == "total" and avg["Способ подсчета"] == "average"


def test_aggregate_month_none_when_no_current_rows():
    rows = [{"Date": "2026-08-01", "__period": "previous", "domain": "nutrition", "Calories": "2000"}]
    assert mt.aggregate_month(rows, "2026-09") is None


def test_aggregate_month_skips_missing_values_in_average():
    rows = [
        {"Date": "2026-09-01", "__period": "current", "Calories": "2000"},
        {"Date": "2026-09-02", "__period": "current", "Calories": None},  # не считается в average
    ]
    total, avg = mt.aggregate_month(rows, "2026-09")
    assert total["Calories"] == 2000.0
    assert avg["Calories"] == 2000.0  # (2000)/1, не /2


def test_compute_trend_flags_strong_shift():
    rows = []
    for i in range(12):
        rows.append({"__period": "previous", "domain": "nutrition", "Calories": str(2000 + (i % 3) * 10)})
    for i in range(12):
        rows.append({"__period": "current", "domain": "nutrition", "Calories": str(2500 + (i % 3) * 10)})
    trends = mt.compute_trend(rows)
    cal = next(t for t in trends if t["metric"] == "Calories")
    assert cal["severity"] == "strong"
    assert cal["prev_month_mean"] == pytest.approx(2010.0, abs=1)
    assert cal["this_month_mean"] == pytest.approx(2510.0, abs=1)


def test_compute_trend_skips_when_below_min_n():
    rows = [{"__period": "previous", "domain": "nutrition", "Calories": "2000"}] * 5
    rows += [{"__period": "current", "domain": "nutrition", "Calories": "2500"}] * 5
    assert mt.compute_trend(rows) == []


def test_compute_trend_direction_interpretation_higher_better():
    rows = [{"__period": "previous", "domain": "nutrition", "Proteins": str(100 + i)} for i in range(12)]
    rows += [{"__period": "current", "domain": "nutrition", "Proteins": str(150 + i)} for i in range(12)]
    trends = mt.compute_trend(rows)
    prot = next(t for t in trends if t["metric"] == "Proteins")
    assert prot["interpretation"] == "улучшение"  # higher_better, выросло


def test_compute_trend_direction_interpretation_lower_better_worsening():
    rows = [{"__period": "previous", "domain": "wellness", "Пульс_ночной_средний": str(50 + i)} for i in range(12)]
    rows += [{"__period": "current", "domain": "wellness", "Пульс_ночной_средний": str(65 + i)} for i in range(12)]
    trends = mt.compute_trend(rows)
    hr = next(t for t in trends if t["metric"] == "Пульс_ночной_средний")
    assert hr["interpretation"] == "ухудшение"  # lower_better, выросло


def test_compute_trend_empty_when_no_rows():
    assert mt.compute_trend([]) == []


def test_build_telegram_text_prefers_strong_over_moderate():
    trends = [
        {"metric": "A", "domain": "nutrition", "severity": "moderate", "interpretation": "улучшение", "prev_month_mean": 1, "this_month_mean": 2},
        {"metric": "B", "domain": "nutrition", "severity": "strong", "interpretation": "ухудшение", "prev_month_mean": 3, "this_month_mean": 1},
    ]
    text = mt.build_telegram_text("2026-08", trends)
    assert "B" in text and "A" not in text
    assert "Сдвигов: 2 (сильных: 1)" in text


# --- запись в Postgres (реальные таблицы, тестовый month-ключ, cleanup) -------

TEST_MONTH = "1999-12"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.month_sum WHERE month = %s", (TEST_MONTH,))
        cur.execute("DELETE FROM health.month_wellness_log WHERE month = %s", (TEST_MONTH,))
        cur.execute("DELETE FROM health.monthly_trend_log WHERE period_month = %s", (TEST_MONTH,))
        conn.commit()


def test_write_month_sum_upserts():
    total = {"User_ID": "x", "Date": TEST_MONTH, "Способ подсчета": "total", "Calories": 60000.0}
    avg = {"User_ID": "x", "Date": TEST_MONTH, "Способ подсчета": "average", "Calories": 2000.0}
    with get_conn() as conn, conn.cursor() as cur:
        mt.write_month_sum(cur, TEST_MONTH, total, avg)
        conn.commit()
        cur.execute("SELECT calc_method, metrics FROM health.month_sum WHERE month = %s ORDER BY calc_method", (TEST_MONTH,))
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "average" and rows[0][1]["Calories"] == 2000.0
    assert rows[1][0] == "total" and rows[1][1]["Calories"] == 60000.0


def test_write_monthly_trend_log_upserts_on_rerun():
    trend = {
        "date_computed": "2026-01-01", "domain": "nutrition", "metric": "Calories",
        "prev_month_mean": 2000.0, "this_month_mean": 2500.0, "z": 2.1,
        "n_current": 12, "n_previous": 12, "severity": "strong",
        "direction": "neutral", "interpretation": "изменение",
    }
    with get_conn() as conn, conn.cursor() as cur:
        mt.write_monthly_trend_log(cur, TEST_MONTH, [trend])
        conn.commit()
        cur.execute("SELECT count(*), z FROM health.monthly_trend_log WHERE period_month = %s GROUP BY z", (TEST_MONTH,))
        row = cur.fetchone()
        assert row[0] == 1 and float(row[1]) == 2.1

    trend2 = dict(trend, z=3.3)
    with get_conn() as conn, conn.cursor() as cur:
        mt.write_monthly_trend_log(cur, TEST_MONTH, [trend2])
        conn.commit()
        cur.execute("SELECT count(*) FROM health.monthly_trend_log WHERE period_month = %s", (TEST_MONTH,))
        assert cur.fetchone() == (1,)  # перезаписал, не задвоил
        cur.execute("SELECT z FROM health.monthly_trend_log WHERE period_month = %s", (TEST_MONTH,))
        assert float(cur.fetchone()[0]) == 3.3


# --- run_once (полностью замоканная оркестрация) -------------------------------

def test_run_once_sends_telegram_when_trends_found(monkeypatch):
    windows = {"current_month": TEST_MONTH, "previous_month": "1999-11"}
    monkeypatch.setattr(mt, "define_month_windows", lambda: windows)
    monkeypatch.setattr(mt, "split_and_tag", lambda rows, field, domain, w: [{"__period": "current", "domain": domain}])
    monkeypatch.setattr(mt, "aggregate_month", lambda rows, m: None)
    fake_trend = [{
        "date_computed": "2026-01-01", "domain": "nutrition", "metric": "Calories",
        "prev_month_mean": 2000.0, "this_month_mean": 2500.0, "z": 2.1,
        "n_current": 12, "n_previous": 12, "severity": "strong", "direction": "neutral", "interpretation": "изменение",
    }]
    monkeypatch.setattr(mt, "compute_trend", lambda rows: fake_trend if rows and rows[0]["domain"] == "nutrition" else [])

    sent = []
    from app import hermes_telegram  # 2026-09-21: алерты -> Hermes, не бот доктора
    monkeypatch.setattr(hermes_telegram, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    mt.run_once()

    assert len(sent) == 1
    assert TEST_MONTH in sent[0][1]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM health.monthly_trend_log WHERE period_month = %s", (TEST_MONTH,))
        assert cur.fetchone() == (1,)


def test_run_once_no_trends_sends_nothing(monkeypatch):
    windows = {"current_month": TEST_MONTH, "previous_month": "1999-11"}
    monkeypatch.setattr(mt, "define_month_windows", lambda: windows)
    monkeypatch.setattr(mt, "split_and_tag", lambda rows, field, domain, w: [])
    monkeypatch.setattr(mt, "aggregate_month", lambda rows, m: None)
    monkeypatch.setattr(mt, "compute_trend", lambda rows: [])

    sent = []
    from app import hermes_telegram  # 2026-09-21: алерты -> Hermes, не бот доктора
    monkeypatch.setattr(hermes_telegram, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    mt.run_once()
    assert sent == []
