"""Мета-отчёт «что на мне работает» (тикет «хвост», 2026-09-26, Часть 3) —
все вердикты по рекомендациям/интервенциям в одном месте: effective/partial/
no_effect/not_adhered/data_gap, с метрикой, окном, конфаундерами, трассировкой
до рекомендации и публикации. Данные уже существуют полностью нормализованными
в card.recommendation_verdict/card.recommendation/card.expectation/
card.publication (verdict_engine/evaluate_recommendation — не тронуты, только
читаем) — считаем на лету при каждом вызове, отдельной таблицы/materialized
view не заводили: на 2026-09-26 всего 3 текущих вердикта, агрегация по ним —
доли миллисекунды, кэшировать нечего (если объём вырастет на порядки —
тогда и обсуждать materialized view, не раньше).

Полная витрина (Vita v2, секция «Программа») — не здесь: этот модуль отдаёт
только данные + API (/outcomes/detail, /outcomes/quarterly) и один триггер
1-го числа с одной строкой в дайджест."""
import logging
import time
from collections import Counter
from datetime import date, timedelta
from typing import Optional

from psycopg import sql

from app.db import get_conn, schema
from app import notify
from app import run_log
from app import timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

MONTHLY_HOUR_VL = 9  # 1-е число, до утренних дайджестов остального контура

_WORKING_VERDICTS = {"effective", "partial"}
_CLOSING_VERDICTS = {"no_effect", "adverse", "not_adhered"}  # то же множество, что recommendations.py — не переизобретаем
_VERDICT_LABEL_RU = {
    "effective": "сработало", "partial": "частично сработало", "no_effect": "не сработало",
    "adverse": "стало хуже", "not_adhered": "не соблюдалось", "data_gap": "не хватило данных",
}


def get_outcomes_detail(cur) -> list[dict]:
    """Один вердикт — одна строка. Только status='current' (не переигранные
    старые вычисления того же цикла — rv.status='superseded' их уже помечает,
    та же логика, что get_loops() в recommendations.py)."""
    cur.execute(
        sql.SQL(
            "SELECT rv.id, rv.rec_id, rv.cycle, rv.ts_computed, rv.verdict, rv.metric_key, "
            "rv.baseline_value, rv.eval_value, rv.confounded, rv.rule_trace, "
            "ex.metric_label, ex.unit, ex.window_days, "
            "rc.title, rc.status AS rec_status, rc.stop_reason, rc.intervention_id, "
            "p.title AS publication_title, p.url AS publication_url "
            "FROM {rv} rv "
            "JOIN {rc} rc ON rc.id = rv.rec_id "
            "LEFT JOIN {ex} ex ON ex.rec_id = rv.rec_id AND ex.cycle = rv.cycle AND ex.role = 'primary' "
            "LEFT JOIN {pub} p ON p.id = rc.publication_id "
            "WHERE rv.status = 'current' "
            "ORDER BY rv.ts_computed DESC"
        ).format(
            rv=sql.Identifier(schema(), "recommendation_verdict"),
            rc=sql.Identifier(schema(), "recommendation"),
            ex=sql.Identifier(schema(), "expectation"),
            pub=sql.Identifier(schema(), "publication"),
        )
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _quarter_bounds(d: date) -> tuple[date, date]:
    q_start_month = ((d.month - 1) // 3) * 3 + 1
    start = date(d.year, q_start_month, 1)
    if q_start_month == 10:
        end = date(d.year + 1, 1, 1)
    else:
        end = date(d.year, q_start_month + 3, 1)
    return start, end


def _prev_quarter_bounds(d: date) -> tuple[date, date]:
    start, _ = _quarter_bounds(d)
    last_day_of_prev_quarter = start - timedelta(days=1)
    return _quarter_bounds(last_day_of_prev_quarter)


def _working_share(rows: list[dict]) -> Optional[float]:
    if not rows:
        return None
    working = sum(1 for r in rows if r["verdict"] in _WORKING_VERDICTS)
    return round(working / len(rows) * 100)


def quarterly_summary(cur, today: Optional[date] = None) -> dict:
    """Счётчики по исходам за текущий квартал, тренд доли "работает"
    (effective+partial) против прошлого квартала, список закрытых с исходом.
    Пустой отчёт (вердиктов за квартал нет) — total=0, caller решает не
    отправлять строку в дайджест (Часть 3.4 тикета)."""
    today = today or timeutil.now_local().date()
    q_start, q_end = _quarter_bounds(today)
    prev_start, prev_end = _prev_quarter_bounds(today)

    detail = get_outcomes_detail(cur)

    def _in_window(r, start, end):
        ts = r["ts_computed"]
        d = ts.date() if hasattr(ts, "date") else ts
        return start <= d < end

    quarter_rows = [r for r in detail if _in_window(r, q_start, q_end)]
    prev_rows = [r for r in detail if _in_window(r, prev_start, prev_end)]

    counts = Counter(r["verdict"] for r in quarter_rows)
    working_share = _working_share(quarter_rows)
    prev_working_share = _working_share(prev_rows)
    trend = None
    if working_share is not None and prev_working_share is not None:
        trend = working_share - prev_working_share

    closed = [
        {"title": r["title"], "verdict": r["verdict"], "metric_label": r["metric_label"] or r["metric_key"],
         "stop_reason": r["stop_reason"]}
        for r in quarter_rows if r["rec_status"] == "closed"
    ]

    return {
        "quarter": {"from": q_start.isoformat(), "to": q_end.isoformat()},
        "total": len(quarter_rows),
        "counts": dict(counts),
        "working_share_pct": working_share,
        "working_share_trend_pts": trend,
        "closed": closed,
    }


def build_digest_line(summary: dict) -> Optional[str]:
    """Часть 3.3-3.4: пустой отчёт -> None (строка в дайджест не идёт).
    Формулировка — ровно из тикета."""
    if summary["total"] == 0:
        return None
    share = summary["working_share_pct"]
    return f"Мета-отчёт обновлён: {summary['total']} вердиктов, доля работающего — {share}%."


def run_once() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        summary = quarterly_summary(cur)
    line = build_digest_line(summary)
    if line:
        notify.notify("outcomes_report", "normal", line)
    logger.info("outcomes_report: готово, вердиктов за квартал=%d, доля работающего=%s",
                summary["total"], summary["working_share_pct"])


def _sleep_until_first_of_month(hour: int) -> None:
    timeutil.sleep_until_local(hour, day_of_month=1)


def run_scheduler() -> None:
    logger.info("outcomes_report scheduler: старт (1-е число, %02d:00 ВЛ)", MONTHLY_HOUR_VL)
    while True:
        try:
            _sleep_until_first_of_month(MONTHLY_HOUR_VL)
            run_once()
            run_log.mark_run("outcomes_report")
        except Exception as e:
            logger.exception("outcomes_report: run_once упал — повтор через сутки")
            alert_on_failure("outcomes_report", e)
            time.sleep(3600)
