"""app/anomaly_detector.py — порт n8n Anomaly_Detector/Correlations
(2026-09-20, группа 3). Юниты на чистый z-score движок + сценарии дедупа/
записи через monkeypatch и реальные health.anomaly_alerted/health.anomaly_log/
health.digest_log/card.anomaly_detector_state (последняя — синглтон,
хардкожена в app/anomaly_detector.py буквально как "card.", в обход
schema()/card_test).

2026-09-24 (ROADMAP 0.7): изоляция через `_isolate_real_schema_writes`
(tests/conftest.py) — раньше здесь были две фикстуры: обычный DELETE по
тестовым датам (безопасно и так, но задача — не полагаться на "убрал за
собой") и save/restore для синглтона card.anomaly_detector_state (после
инцидента с health.anomaly_log в этом же файле). Обе избыточны под
изоляцией — ничего не коммитится, восстанавливать нечего."""
import json
from datetime import date, timedelta

import pytest

from app import anomaly_detector as ad
from app.db import get_conn

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")

METRICS = [{"key": "m", "label": "Метрика", "direction": "higher_better", "min_abs_delta": 1}]


def _rows(base_date: date, values: list, key="m"):
    out = []
    for i, v in enumerate(values):
        d = (base_date + timedelta(days=i)).isoformat()
        out.append({"Дата": d, key: v})
    return out


# --- чистые хелперы -----------------------------------------------------------

def test_to_number_parses_comma_and_percent():
    assert ad._to_number("24,3") == 24.3
    assert ad._to_number("85%") == 85
    assert ad._to_number("") is None
    assert ad._to_number(None) is None
    assert ad._to_number("не число") is None


def test_parse_date_handles_iso_and_truncates_time():
    assert ad._parse_date("2026-09-20") == date(2026, 9, 20)
    assert ad._parse_date("2026-09-20T15:30") == date(2026, 9, 20)
    assert ad._parse_date(None) is None
    assert ad._parse_date("не дата") is None


# --- detect_anomalies -----------------------------------------------------

def test_detect_anomalies_flags_strong_outlier():
    base = date(2026, 8, 1)
    # 10 дней с небольшим разбросом (baseline ~10, std>0), последний день — резкий скачок
    values = [9, 10, 11, 10, 9, 11, 10, 9, 10, 11, 30]
    days = ad.detect_anomalies(_rows(base, values), METRICS)
    latest = days[-1]
    assert latest["is_latest"] is True
    assert latest["has_anomalies"] is True
    a = latest["anomalies"][0]
    assert a["severity"] == "strong"
    assert a["direction"] == "higher_better"
    assert a["interpretation"] == "улучшение"  # higher_better + z>0 = не "ухудшение"


def test_detect_anomalies_no_anomaly_when_baseline_too_short():
    base = date(2026, 8, 1)
    values = [10, 10, 30]  # baseline < min_points(4) для окна 7д
    days = ad.detect_anomalies(_rows(base, values), METRICS)
    assert days[-1]["has_anomalies"] is False


def test_detect_anomalies_abs_gate_blocks_noise():
    # почти-константная метрика с огромным min_abs_delta — z может быть большим,
    # но абсолютное отклонение слишком мало, чтобы считаться аномалией
    metrics = [{"key": "m", "label": "M", "direction": "neutral", "min_abs_delta": 1000}]
    base = date(2026, 8, 1)
    values = [100.0] * 10 + [100.5]
    days = ad.detect_anomalies(_rows(base, values), metrics)
    assert days[-1]["has_anomalies"] is False


def test_detect_anomalies_worse_direction_for_lower_better():
    metrics = [{"key": "m", "label": "M", "direction": "lower_better", "min_abs_delta": 1}]
    base = date(2026, 8, 1)
    values = [9, 10, 11, 10, 9, 11, 10, 9, 10, 11, 30]  # рост при lower_better = ухудшение
    days = ad.detect_anomalies(_rows(base, values), metrics)
    assert days[-1]["anomalies"][0]["interpretation"] == "ухудшение"


def test_detect_anomalies_skips_rows_without_date():
    rows = [{"Дата": None, "m": 10}, {"Дата": "2026-08-01", "m": 10}]
    days = ad.detect_anomalies(rows, METRICS)
    assert len(days) == 1


def test_detect_anomalies_is_percent_normalizes_fraction():
    metrics = [{"key": "p", "label": "P", "direction": "higher_better", "min_abs_delta": 1, "is_percent": True}]
    rows = [{"Дата": "2026-08-01", "p": 0.9}, {"Дата": "2026-08-02", "p": 1.5}]
    days = ad.detect_anomalies(rows, metrics)
    # 0.9 -> 90 (<=1.5 правило), 1.5 -> 150 (граница включительно тоже *100 — сверено с оригиналом v<=1.5)
    assert days[0]["date"] == "2026-08-01"


# --- _load_metrics --------------------------------------------------------

def test_load_metrics_fallback_when_sheets_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("нет сети")
    monkeypatch.setattr("app.sheets_client.get_values", boom)
    metrics = ad._load_metrics(cur=None)
    assert metrics == ad._FALLBACK_METRICS


def test_load_metrics_parses_real_sheet_shape(monkeypatch):
    header = ["fkey", "col", "label", "unit", "direction", "min_abs_delta", "target_min", "target_max", "target_label", "kind"]
    rows = [
        header,
        ["hrv", "ВСР_ночная", "ВСР ночью", "мс", "higher_better", "6", "", "", "", "baseline"],
        ["respiration", "Дыхание_ночь_среднее", "Дыхание ночью", "/мин", "reference", "", "12", "20", "12–20/мин", "reference"],
    ]
    monkeypatch.setattr("app.sheets_client.get_values", lambda *a, **kw: rows)
    metrics = ad._load_metrics(cur=None)
    assert len(metrics) == 1  # reference-строка отфильтрована
    assert metrics[0]["key"] == "ВСР_ночная"
    assert metrics[0]["min_abs_delta"] == 6.0


def test_load_metrics_fallback_when_sheet_empty(monkeypatch):
    monkeypatch.setattr("app.sheets_client.get_values", lambda *a, **kw: [])
    assert ad._load_metrics(cur=None) == ad._FALLBACK_METRICS


# --- дедуп / запись (реальная health.anomaly_alerted + health.anomaly_log) ----

TEST_DAY = "1999-12-31"


def test_mark_and_check_alerted_roundtrip():
    with get_conn() as conn, conn.cursor() as cur:
        assert ad._already_alerted(cur, TEST_DAY, "Метрика") is False
        ad._mark_alerted(cur, TEST_DAY, "Метрика")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        assert ad._already_alerted(cur, TEST_DAY, "Метрика") is True


def test_write_anomaly_log_upserts():
    anomalies = [{"metric": "m", "label": "M", "value": 5, "z": 2.5, "window": "7д",
                  "severity": "strong", "baseline_mean": 1.0, "direction": "neutral", "interpretation": "отклонение"}]
    with get_conn() as conn, conn.cursor() as cur:
        ad.write_anomaly_log(cur, TEST_DAY, anomalies)
        conn.commit()
        cur.execute("SELECT anomaly_count, strong_count FROM health.anomaly_log WHERE date = %s", (TEST_DAY,))
        assert cur.fetchone() == (1, 1)

    # повторная запись перезаписывает, не дублирует
    with get_conn() as conn, conn.cursor() as cur:
        ad.write_anomaly_log(cur, TEST_DAY, [])
        conn.commit()
        cur.execute("SELECT anomaly_count FROM health.anomaly_log WHERE date = %s", (TEST_DAY,))
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0] == (0,)


# --- build_weekly_digest ---------------------------------------------------

def test_build_weekly_digest_groups_by_metric():
    days = [
        {"date": "2026-09-01", "anomalies": [{"metric": "m", "label": "M", "severity": "strong", "z": 2.1}]},
        {"date": "2026-09-03", "anomalies": [{"metric": "m", "label": "M", "severity": "moderate", "z": 1.6}]},
        {"date": "2026-09-06", "anomalies": []},
    ]
    d = ad.build_weekly_digest(days)
    assert d["period_start"] == "2026-09-01"
    assert d["period_end"] == "2026-09-06"
    assert d["total_anomalies"] == 2
    assert d["strong_anomalies"] == 1
    assert "M: аномалия 2×" in d["metric_summary_lines"][0]


def test_build_weekly_digest_none_when_no_days():
    assert ad.build_weekly_digest([]) is None


def test_build_weekly_digest_excludes_days_outside_window():
    days = [
        {"date": "2026-08-01", "anomalies": [{"metric": "m", "label": "M", "severity": "strong", "z": 3}]},
        {"date": "2026-09-06", "anomalies": []},
    ]
    d = ad.build_weekly_digest(days)
    assert d["total_anomalies"] == 0  # 01.08 вне 7-дневного окна до 06.09


# --- mark_daily_check_ran -----------------------------------------------------
# card.anomaly_detector_state — singleton (id=1), хардкожен в
# app/anomaly_detector.py буквально как "card." (не через schema()), поэтому
# писал в БОЕВОЙ card даже под CARD_PG_SCHEMA=card_test. До 2026-09-24 здесь
# был save/restore (после инцидента с health.anomaly_log в этом же файле) —
# под _isolate_real_schema_writes избыточен, ничего не коммитится.

def test_mark_daily_check_ran_writes_state():
    """2026-09-22: отдельная отметка о прогоне (не health.anomaly_log — та
    пишется только при находках) — без неё ложный алерт system_check.py
    после любой 'чистой' серии дней (см. докстринг mark_daily_check_ran)."""
    with get_conn() as conn, conn.cursor() as cur:
        ad.mark_daily_check_ran(cur, "2020-01-15")
        conn.commit()
        cur.execute("SELECT last_day_checked FROM card.anomaly_detector_state WHERE id = 1")
        assert str(cur.fetchone()[0]) == "2020-01-15"

        ad.mark_daily_check_ran(cur, "2020-01-16")  # upsert, не вторая строка
        conn.commit()
        cur.execute("SELECT count(*), max(last_day_checked) FROM card.anomaly_detector_state")
        count, last = cur.fetchone()
        assert count == 1
        assert str(last) == "2020-01-16"


# --- run_daily_check / run_weekly_digest (оркестрация, всё внешнее замокано) --

def test_run_daily_check_no_anomalies_sends_nothing(monkeypatch):
    monkeypatch.setattr(ad, "_fetch_daily_and_metrics", lambda cur: ([], METRICS))
    sent = []
    monkeypatch.setattr(ad.telegram, "send_message", lambda *a: sent.append(a))
    ad.run_daily_check()
    assert sent == []


def test_run_daily_check_clean_day_still_marks_state(monkeypatch):
    """Регрессия 2026-09-22: 'аномалий нет' — законный итог, но детектор
    ДОЛЖЕН отметиться как проверивший этот день, иначе system_check.py не
    отличит 'чисто' от 'вообще не запускался'."""
    rows = _rows(date.today(), [10, 10, 10, 10])
    latest_date = rows[-1]["Дата"]
    monkeypatch.setattr(ad, "_fetch_daily_and_metrics", lambda cur: (rows, METRICS))
    monkeypatch.setattr(ad.telegram, "send_message", lambda *a: (_ for _ in ()).throw(AssertionError("не должен слать")))
    ad.run_daily_check()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_day_checked FROM card.anomaly_detector_state WHERE id = 1")
        assert str(cur.fetchone()[0]) == latest_date


def test_run_daily_check_sends_once_then_dedups_rerun(monkeypatch):
    # даты должны быть БЛИЗКИ к реальному "сегодня" — _prune_alerted() чистит
    # дедуп-записи старше ANOMALY_ALERT_KEEP_DAYS по РЕАЛЬНОМУ wall-clock now(),
    # что в проде всегда верно (latest = вчера/сегодня), но с искусственно
    # старыми тестовыми датами прунинг стёр бы дедуп-запись раньше времени.
    #
    # ИНЦИДЕНТ 2026-09-21/22 (найдено по репорту Влада): latest_date здесь —
    # РЕАЛЬНОЕ "сегодня", то есть тот же день, за который в проде вполне может
    # уже лежать настоящая находка (ровно это и случилось — полный прогон
    # тестов в тот день, когда для health.anomaly_log была живая запись 21.09,
    # съел её безусловным DELETE в finally). Теперь finally восстанавливает
    # то, что было ДО теста, а не просто чистит за собой в предположении, что
    # "там ничего не было".
    base = date.today() - timedelta(days=10)
    rows = _rows(base, [9, 10, 11, 10, 9, 11, 10, 9, 10, 11, 30])
    latest_date = rows[-1]["Дата"]
    monkeypatch.setattr(ad, "_fetch_daily_and_metrics", lambda cur: (rows, METRICS))
    sent = []
    monkeypatch.setattr(ad.telegram, "send_message", lambda *a: sent.append(a))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT anomaly_count, strong_count, raw_anomalies, created_at FROM health.anomaly_log WHERE date = %s",
            (latest_date,),
        )
        preexisting_log = cur.fetchone()
        cur.execute("SELECT key, alert_date FROM health.anomaly_alerted WHERE key LIKE %s", (f"{latest_date}%",))
        preexisting_alerted = cur.fetchall()

    try:
        ad.run_daily_check()
        assert len(sent) == 1
        assert "Метрика" in sent[0][1]

        sent.clear()
        ad.run_daily_check()  # тот же день, та же аномалия — уже отмечена, повтор не шлём
        assert sent == [], "повторный прогон на тот же день не должен дублировать Telegram-алерт"
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM health.anomaly_alerted WHERE key LIKE %s", (f"{latest_date}%",))
            cur.execute("DELETE FROM health.anomaly_log WHERE date = %s", (latest_date,))
            if preexisting_log:
                # 2026-09-23: preexisting_log пришёл из SELECT — psycopg уже
                # ДЕСЕРИАЛИЗОВАЛ jsonb raw_anomalies в Python list/dict; INSERT
                # обратно тем же значением без json.dumps() падал с
                # "cannot adapt type 'dict'" (raw_anomalies — jsonb, нужен
                # текст + ::jsonb, тот же приём, что в write_anomaly_log()).
                anomaly_count, strong_count, raw_anomalies, created_at = preexisting_log
                cur.execute(
                    "INSERT INTO health.anomaly_log (date, anomaly_count, strong_count, raw_anomalies, created_at) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s)",
                    (latest_date, anomaly_count, strong_count, json.dumps(raw_anomalies, ensure_ascii=False), created_at),
                )
            for key, alert_date in preexisting_alerted:
                cur.execute(
                    "INSERT INTO health.anomaly_alerted (key, alert_date) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (key, alert_date),
                )
            conn.commit()


# --- _append_digest_row: постепенный отказ от Sheets (2026-09-23, категория A) —
# health.digest_log вместо append_row в Google Sheets ---------------------------

def test_append_digest_row_writes_to_postgres_and_upserts():
    d = {
        "period_start": "1999-12-25", "period_end": "1999-12-31",
        "days_with_data": 7, "total_anomalies": 3, "strong_anomalies": 1,
        "metric_summary_lines": ["Сон: аномалия 2× за неделю (1999-12-26, 1999-12-27)"],
        "days_detail": [{"date": "1999-12-26", "anomaly_count": 1, "anomalies": []}],
    }
    ad._append_digest_row(d)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT period_end, days_with_data, total_anomalies, strong_anomalies, metric_summary, days_detail "
            "FROM health.digest_log WHERE period_start = %s", (d["period_start"],),
        )
        row = cur.fetchone()
    assert row[0].isoformat() == d["period_end"]
    assert row[1:4] == (7, 3, 1)
    assert "Сон" in row[4]
    assert row[5][0]["date"] == "1999-12-26"

    # Повторный прогон той же недели — UPSERT, не дубль-строка (period_start — PK).
    d["total_anomalies"] = 5
    ad._append_digest_row(d)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), max(total_anomalies) FROM health.digest_log WHERE period_start = %s",
                    (d["period_start"],))
        count, total = cur.fetchone()
    assert count == 1
    assert total == 5


def test_run_weekly_digest_no_data_sends_nothing(monkeypatch):
    monkeypatch.setattr(ad, "_fetch_daily_and_metrics", lambda cur: ([], METRICS))
    sent = []
    monkeypatch.setattr(ad.telegram, "send_message", lambda *a: sent.append(a))
    ad.run_weekly_digest()
    assert sent == []


def test_run_weekly_digest_sends_and_appends(monkeypatch):
    base = date(2026, 8, 1)
    rows = _rows(base, [10] * 10 + [30])
    monkeypatch.setattr(ad, "_fetch_daily_and_metrics", lambda cur: (rows, METRICS))
    appended = []
    monkeypatch.setattr(ad, "_append_digest_row", lambda d: appended.append(d))
    sent = []
    monkeypatch.setattr(ad.telegram, "send_message", lambda *a: sent.append(a))

    ad.run_weekly_digest()
    assert len(sent) == 1
    assert "Недельный дайджест" in sent[0][1]
    assert len(appended) == 1
