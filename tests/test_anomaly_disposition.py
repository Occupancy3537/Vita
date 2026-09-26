"""«Мост аномалия → действие» (2026-09-25, G5 VISION) — app/anomaly_disposition.py.
Юниты на реальную (изолированную) card.anomaly_disposition/health.anomaly_log/
health.investigations — dispose()/series-эскалация/suppress-окно/дайджест-строка."""
import json
from datetime import date, timedelta

import pytest

from app import anomaly_disposition as ad
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")

TODAY = date(2026, 9, 20)


def _seed_anomaly_log(day: date, anomalies: list[dict]):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO health.anomaly_log (date, anomaly_count, strong_count, raw_anomalies) "
            "VALUES (%s, %s, %s, %s::jsonb) "
            "ON CONFLICT (date) DO UPDATE SET raw_anomalies = EXCLUDED.raw_anomalies",
            (day.isoformat(), len(anomalies), sum(1 for a in anomalies if a["severity"] == "strong"),
             json.dumps(anomalies, ensure_ascii=False)),
        )
        conn.commit()


# ─────── создание строки диспозиции — идемпотентно по (metric_key, date) ───────

def test_create_disposition_row_idempotent_same_day():
    with get_conn() as conn, conn.cursor() as cur:
        id1, created1 = ad.create_disposition_row(cur, "hrv", "ВСР", TODAY.isoformat(), "strong")
        id2, created2 = ad.create_disposition_row(cur, "hrv", "ВСР", TODAY.isoformat(), "strong")
        conn.commit()
    assert created1 is True and created2 is False
    assert id1 == id2
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.anomaly_disposition WHERE metric_key = 'hrv' AND date = %s",
                    (TODAY.isoformat(),))
        assert cur.fetchone()[0] == 1


def test_create_disposition_row_defaults_to_pending():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "sleep_min", "Сон", TODAY.isoformat(), "strong")
        conn.commit()
        cur.execute(f"SELECT disposition, disposed_ts FROM {schema()}.anomaly_disposition WHERE metric_key='sleep_min' AND date=%s",
                    (TODAY.isoformat(),))
        disposition, disposed_ts = cur.fetchone()
    assert disposition == "pending" and disposed_ts is None


# ─────── Часть 1.2 — серия: 3 moderate за 7 дней эскалирует ───────

def test_series_escalates_at_three_moderate_in_window():
    base = TODAY - timedelta(days=6)
    for i in range(3):
        _seed_anomaly_log(base + timedelta(days=i * 3), [{"metric": "stress", "severity": "moderate"}])
    with get_conn() as conn, conn.cursor() as cur:
        count = ad.check_series_escalation(cur, "stress", TODAY.isoformat())
    assert count >= ad.SERIES_MIN_COUNT


def test_single_moderate_is_not_a_series():
    _seed_anomaly_log(TODAY, [{"metric": "stress", "severity": "moderate"}])
    with get_conn() as conn, conn.cursor() as cur:
        count = ad.check_series_escalation(cur, "stress", TODAY.isoformat())
    assert count < ad.SERIES_MIN_COUNT


def test_series_window_excludes_older_than_7_days():
    _seed_anomaly_log(TODAY - timedelta(days=10), [{"metric": "stress", "severity": "moderate"}])
    _seed_anomaly_log(TODAY - timedelta(days=9), [{"metric": "stress", "severity": "moderate"}])
    _seed_anomaly_log(TODAY, [{"metric": "stress", "severity": "moderate"}])
    with get_conn() as conn, conn.cursor() as cur:
        count = ad.check_series_escalation(cur, "stress", TODAY.isoformat())
    assert count == 1  # только сегодняшняя — две другие вне 7-дневного окна


# ─────── Часть 3.1 — suppress: молчит в окне, но пишется в лог ───────

def test_suppress_is_active_within_window():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "rhr", "RHR", (TODAY - timedelta(days=5)).isoformat(), "strong",
                                  disposition="suppress", reason="известное ОРВИ")
        cur.execute(f"UPDATE {schema()}.anomaly_disposition SET suppress_until = %s WHERE metric_key='rhr'",
                    ((TODAY + timedelta(days=25)).isoformat(),))
        conn.commit()
        active = ad.active_suppression(cur, "rhr", TODAY.isoformat())
    assert active is not None and active["reason"] == "известное ОРВИ"


def test_suppress_expired_is_not_active():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "rhr2", "RHR2", (TODAY - timedelta(days=40)).isoformat(), "strong",
                                  disposition="suppress", reason="старое")
        cur.execute(f"UPDATE {schema()}.anomaly_disposition SET suppress_until = %s WHERE metric_key='rhr2'",
                    ((TODAY - timedelta(days=10)).isoformat(),))
        conn.commit()
        active = ad.active_suppression(cur, "rhr2", TODAY.isoformat())
    assert active is None


# ─────── dispose() — назначение через инструмент доктора ───────

def test_dispose_acknowledge_updates_pending_row():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "vo2max", "VO2 Max", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "VO2", "acknowledge", reason="знаю, был перелёт")
        conn.commit()
        cur.execute(f"SELECT disposition, reason FROM {schema()}.anomaly_disposition WHERE metric_key='vo2max'")
        row = cur.fetchone()
    assert result["ok"] is True and result["count"] == 1
    assert row == ("acknowledge", "знаю, был перелёт")


def test_dispose_suppress_sets_window():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "stress2", "Стресс", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "Стресс", "suppress", reason="дедлайн на работе", window_days=14, today=TODAY)
        conn.commit()
        cur.execute(f"SELECT suppress_until FROM {schema()}.anomaly_disposition WHERE metric_key='stress2'")
        suppress_until = cur.fetchone()[0]
    assert result["ok"] is True
    assert suppress_until == TODAY + timedelta(days=14)


def test_dispose_suppress_default_window_is_30_days():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "stress3", "Стресс3", TODAY.isoformat(), "strong")
        conn.commit()
        ad.dispose(cur, "Стресс3", "suppress", today=TODAY)
        conn.commit()
        cur.execute(f"SELECT suppress_until FROM {schema()}.anomaly_disposition WHERE metric_key='stress3'")
        suppress_until = cur.fetchone()[0]
    assert suppress_until == TODAY + timedelta(days=ad.SUPPRESS_DEFAULT_DAYS)


def test_dispose_unknown_metric_returns_honest_error():
    with get_conn() as conn, conn.cursor() as cur:
        result = ad.dispose(cur, "полностью выдуманная метрика", "acknowledge")
    assert result["ok"] is False and "не найдено" in result["error"]


def test_dispose_invalid_disposition_rejected():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "hrv4", "ВСР4", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "ВСР4", "pending")
    assert result["ok"] is False


# ─────── investigate — гипотезы сохраняются (Часть 2.4) ───────

def test_dispose_investigate_saves_hypotheses():
    hyps = [
        {"hypothesis": "поздний алкоголь снизил ВСР", "differentiator": "если в ночь без алкоголя ВСР вернётся — подтверждено"},
        {"hypothesis": "недосып из-за перелёта", "differentiator": "если через 3 обычные ночи ВСР не восстановится — не это"},
    ]
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "hrv5", "ВСР5", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "ВСР5", "investigate", hypotheses=hyps)
        conn.commit()
        cur.execute(f"SELECT hypotheses, disposition FROM {schema()}.anomaly_disposition WHERE metric_key='hrv5'")
        stored_hyps, disposition = cur.fetchone()
    assert result["ok"] is True
    assert disposition == "investigate"
    assert len(stored_hyps) == 2
    assert stored_hyps[0]["hypothesis"] == "поздний алкоголь снизил ВСР"
    assert "если" in stored_hyps[0]["differentiator"]


def test_dispose_investigate_opens_investigation_when_none_open():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {ad._HEALTH_SCHEMA}.investigations SET status = 'report_ready' WHERE lower(status) = 'open'")
        ad.create_disposition_row(cur, "hrv6", "ВСР6", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "ВСР6", "investigate", reason="аномалия ВСР",
                            hypotheses=[{"hypothesis": "H1", "differentiator": "D1"}])
        conn.commit()
        cur.execute(f"SELECT investigation_id FROM {schema()}.anomaly_disposition WHERE metric_key='hrv6'")
        inv_id = cur.fetchone()[0]
    assert result["ok"] is True
    assert result["investigation_id"] is not None
    assert inv_id == result["investigation_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT trigger, status FROM {ad._HEALTH_SCHEMA}.investigations WHERE inv_id = %s", (inv_id,))
        trigger, status = cur.fetchone()
    assert trigger == "anomaly:ВСР6" and status == "open"


def test_dispose_investigate_degrades_gracefully_when_already_open():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {ad._HEALTH_SCHEMA}.investigations (inv_id, opened, updated, status, trigger) "
            "VALUES ('test-already-open', CURRENT_DATE, CURRENT_DATE, 'open', 'test') "
            "ON CONFLICT (inv_id) DO UPDATE SET status = 'open'"
        )
        ad.create_disposition_row(cur, "hrv7", "ВСР7", TODAY.isoformat(), "strong")
        conn.commit()
        result = ad.dispose(cur, "ВСР7", "investigate", hypotheses=[{"hypothesis": "H", "differentiator": "D"}])
        conn.commit()
        cur.execute(f"SELECT disposition, hypotheses, investigation_id FROM {schema()}.anomaly_disposition WHERE metric_key='hrv7'")
        disposition, hyps, inv_id = cur.fetchone()
    assert result["ok"] is True
    assert result["investigation_id"] is None
    assert disposition == "investigate"  # диспозиция всё равно применена
    assert len(hyps) == 1                # гипотезы всё равно сохранены (Часть 2.4)
    assert inv_id is None                # но отдельное расследование НЕ открыто


# ─────── Часть 4.1 — замыкание: расследование закрылось -> explained ───────

def test_sync_resolved_investigations_marks_explained():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {ad._HEALTH_SCHEMA}.investigations (inv_id, opened, updated, status, trigger) "
            "VALUES ('test-resolved-inv', CURRENT_DATE, CURRENT_DATE, 'report_ready', 'test') "
            "ON CONFLICT (inv_id) DO UPDATE SET status = 'report_ready'"
        )
        _id, _ = ad.create_disposition_row(cur, "hrv8", "ВСР8", TODAY.isoformat(), "strong", disposition="investigate")
        cur.execute(f"UPDATE {schema()}.anomaly_disposition SET investigation_id = 'test-resolved-inv' WHERE id = %s", (_id,))
        conn.commit()
        n = ad.sync_resolved_investigations(cur)
        conn.commit()
        cur.execute(f"SELECT disposition FROM {schema()}.anomaly_disposition WHERE id = %s", (_id,))
        disposition = cur.fetchone()[0]
    assert n >= 1
    assert disposition == "explained"


def test_sync_resolved_investigations_leaves_open_ones_alone():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {ad._HEALTH_SCHEMA}.investigations (inv_id, opened, updated, status, trigger) "
            "VALUES ('test-still-open-inv', CURRENT_DATE, CURRENT_DATE, 'open', 'test') "
            "ON CONFLICT (inv_id) DO UPDATE SET status = 'open'"
        )
        _id, _ = ad.create_disposition_row(cur, "hrv9", "ВСР9", TODAY.isoformat(), "strong", disposition="investigate")
        cur.execute(f"UPDATE {schema()}.anomaly_disposition SET investigation_id = 'test-still-open-inv' WHERE id = %s", (_id,))
        conn.commit()
        ad.sync_resolved_investigations(cur)
        conn.commit()
        cur.execute(f"SELECT disposition FROM {schema()}.anomaly_disposition WHERE id = %s", (_id,))
        disposition = cur.fetchone()[0]
    assert disposition == "investigate"  # ещё открыто — не трогаем


# ─────── Часть 1.4 — pending > 24ч в дайджест, одной строкой ───────

def test_pending_older_than_24h_included():
    with get_conn() as conn, conn.cursor() as cur:
        _id, _ = ad.create_disposition_row(cur, "old_metric", "Старая метрика", TODAY.isoformat(), "strong")
        cur.execute(f"UPDATE {schema()}.anomaly_disposition SET created_ts = now() - interval '48 hours' WHERE id = %s", (_id,))
        conn.commit()
        pending = ad.pending_older_than(cur, hours=24)
    assert any(p["metric_key"] == "old_metric" for p in pending)


def test_pending_fresh_not_included():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "fresh_metric", "Свежая метрика", TODAY.isoformat(), "strong")
        conn.commit()
        pending = ad.pending_older_than(cur, hours=24)
    assert not any(p["metric_key"] == "fresh_metric" for p in pending)


def test_format_pending_digest_line_one_line_multiple_metrics():
    line = ad.format_pending_digest_line([
        {"metric_key": "hrv", "metric_label": "ВСР", "date": "2026-09-18", "severity": "strong"},
        {"metric_key": "sleep_min", "metric_label": "Сон", "date": "2026-09-19", "severity": "strong"},
    ])
    assert line is not None
    assert "\n" not in line  # одна строка (Часть 1.4)
    assert "ВСР" in line and "Сон" in line
    assert ad.REPLY_HINT in line


def test_format_pending_digest_line_empty_is_none():
    assert ad.format_pending_digest_line([]) is None


# ─────── weekly_fates_summary (Часть 4.2) ───────

def test_weekly_fates_summary_includes_recent_dispositions():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "fate1", "Метрика1", TODAY.isoformat(), "strong", disposition="acknowledge")
        conn.commit()
        summary = ad.weekly_fates_summary(cur, (TODAY - timedelta(days=7)).isoformat())
    assert any(f["metric"] == "Метрика1" and f["disposition"] == "acknowledge" for f in summary)


# ─────── pending() (Часть 1.1 «Пересборка вычитанием», 2026-09-26) — лента решений ───────

def test_pending_lists_only_pending_rows():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "feed_pending", "В ленте", TODAY.isoformat(), "strong")
        ad.create_disposition_row(cur, "feed_acked", "Не в ленте", TODAY.isoformat(), "strong")
        ad.dispose(cur, "feed_acked", "acknowledge")
        conn.commit()
        rows = ad.pending(cur)
    keys = {r["metric_key"] for r in rows}
    assert "feed_pending" in keys
    assert "feed_acked" not in keys


def test_pending_empty_when_nothing_pending():
    with get_conn() as conn, conn.cursor() as cur:
        assert ad.pending(cur) == []


# ─────── POST /dashboard/dispose («Пересборка вычитанием», Часть 1.1) —────────
# кнопка на карточке решения дёргает ТУ ЖЕ dispose(), что и Dispose_Anomaly.

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
_TOKEN = "test-dashboard-token-not-prod"


def test_dashboard_dispose_endpoint_updates_disposition():
    with get_conn() as conn, conn.cursor() as cur:
        ad.create_disposition_row(cur, "dash_dispose_ok", "Кнопка ленты", TODAY.isoformat(), "strong")
        conn.commit()
    r = client.post("/dashboard/dispose", json={
        "token": _TOKEN, "metric": "dash_dispose_ok", "disposition": "acknowledge", "reason": "известно",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["disposition"] == "acknowledge"
    with get_conn() as conn, conn.cursor() as cur:
        rows = ad.pending(cur)
    assert "dash_dispose_ok" not in {r["metric_key"] for r in rows}


def test_dashboard_dispose_unknown_metric_is_404():
    r = client.post("/dashboard/dispose", json={
        "token": _TOKEN, "metric": "no_such_metric_at_all", "disposition": "acknowledge",
    })
    assert r.status_code == 404


def test_dashboard_dispose_invalid_disposition_is_422():
    r = client.post("/dashboard/dispose", json={
        "token": _TOKEN, "metric": "whatever", "disposition": "pending",
    })
    assert r.status_code == 422


def test_dashboard_dispose_wrong_token_forbidden():
    r = client.post("/dashboard/dispose", json={
        "token": "wrong", "metric": "whatever", "disposition": "acknowledge",
    })
    assert r.status_code == 403
