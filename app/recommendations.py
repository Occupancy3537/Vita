"""
Phase 3 — рекомендации как объекты (П3). Закрывает находку №1: рекомендация теперь
строка с id, а не JSON-блок, перепарсиваемый из текста при каждом пересчёте кэша.

Три операции:
- sync: создать/дедуп rc_ (+ex_, если measurable) по source_ref.
- evaluate: посчитать вердикт для одной rc_ через verdict_engine, записать rv_.
- loops: отдать дашборду то же самое, что раньше строил парсер прозы — но из rv_.
"""
import json
from datetime import datetime, timedelta
from typing import Optional

from psycopg import sql
from pydantic import BaseModel
from ulid import ULID

from app.db import get_conn, schema
from app.verdict_engine import Expectation, Fact, evaluate as run_verdict_engine


class RecommendationSyncRequest(BaseModel):
    title: str
    action: Optional[str] = None
    rationale: Optional[str] = None
    kind: Optional[str] = None
    source_ref: str
    origin: str = "advisor"
    started_ts: datetime
    metric_key: Optional[str] = None
    metric_label: Optional[str] = None
    unit: Optional[str] = None
    direction: Optional[str] = None  # up | down
    magnitude: Optional[float] = None
    window_days: int = 7
    lag_days: int = 1
    baseline_days: int = 7


class RecommendationSyncResponse(BaseModel):
    id: str
    created: bool
    measurable: bool


def sync_recommendation(req: RecommendationSyncRequest) -> RecommendationSyncResponse:
    table = sql.Identifier(schema(), "recommendation")
    ex_table = sql.Identifier(schema(), "expectation")
    new_id = f"rc_{ULID()}"
    provenance = json.dumps({
        "origin": req.origin, "source_id": None, "extraction": None,
        "model": None, "prompt_version": None, "source_ref": req.source_ref,
    })

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, ts_event, provenance, verification, title, action, rationale, kind, status, started_ts, cycle) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, 'active', %s, 1) "
                    "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
                ).format(table=table),
                (new_id, req.started_ts, provenance, req.title, req.action, req.rationale, req.kind, req.started_ts),
            )
            row = cur.fetchone()
            created = row is not None
            rc_id = row[0] if created else None

            if not created:
                cur.execute(
                    sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s").format(table=table),
                    (req.source_ref,),
                )
                rc_id = cur.fetchone()[0]

            measurable = bool(req.metric_key and req.direction and req.magnitude is not None)
            if created and measurable:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {table} (id, rec_id, cycle, metric_key, metric_label, unit, type, direction, magnitude, window_days, lag_days, baseline_days, role) "
                        "VALUES (%s, %s, 1, %s, %s, %s, 'delta_abs', %s, %s, %s, %s, %s, 'primary')"
                    ).format(table=ex_table),
                    (f"ex_{ULID()}", rc_id, req.metric_key, req.metric_label, req.unit,
                     req.direction, req.magnitude, req.window_days, req.lag_days, req.baseline_days),
                )
        conn.commit()
    return RecommendationSyncResponse(id=rc_id, created=created, measurable=measurable)


class EvaluateResponse(BaseModel):
    evaluated: bool
    verdict: Optional[str] = None
    reason: Optional[str] = None


def evaluate_recommendation(rec_id: str) -> EvaluateResponse:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT started_ts FROM {table} WHERE id = %s")
                .format(table=sql.Identifier(schema(), "recommendation")),
                (rec_id,),
            )
            rc_row = cur.fetchone()
            if rc_row is None:
                return EvaluateResponse(evaluated=False, reason="recommendation не найдена")
            started_ts = rc_row[0]

            cur.execute(
                sql.SQL(
                    "SELECT id, cycle, metric_key, metric_label, unit, direction, magnitude, window_days, lag_days, baseline_days "
                    "FROM {table} WHERE rec_id = %s AND role = 'primary' ORDER BY created_ts DESC LIMIT 1"
                ).format(table=sql.Identifier(schema(), "expectation")),
                (rec_id,),
            )
            ex_row = cur.fetchone()
            if ex_row is None:
                return EvaluateResponse(evaluated=False, reason="нет primary expectation — рекомендация не измерима")

            ex_id, cycle, metric_key, metric_label, unit, direction, magnitude, window_days, lag_days, baseline_days = ex_row
            ex = Expectation(metric_key=metric_key, type="delta_abs", direction=direction, magnitude=float(magnitude),
                              window_days=window_days, lag_days=lag_days, baseline_days=baseline_days)

            fact_from = started_ts - timedelta(days=max(baseline_days, 90))
            fact_to = started_ts + timedelta(days=lag_days + window_days)
            cur.execute(
                sql.SQL("SELECT ts_event, value_num FROM {table} WHERE metric_key = %s AND ts_event >= %s AND ts_event < %s AND value_num IS NOT NULL ORDER BY ts_event")
                .format(table=sql.Identifier(schema(), "fact")),
                (metric_key, fact_from, fact_to),
            )
            facts = [Fact(ts_event=t, value_num=float(v)) for t, v in cur.fetchall()]

            result = run_verdict_engine(started_ts, ex, facts)

            rv_table = sql.Identifier(schema(), "recommendation_verdict")
            cur.execute(
                sql.SQL("UPDATE {table} SET status = 'superseded' WHERE rec_id = %s AND cycle = %s AND status = 'current'")
                .format(table=rv_table),
                (rec_id, cycle),
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, rec_id, cycle, engine_version, verdict, metric_key, baseline_value, eval_value, personal_sigma, coverage, rule_trace, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'current')"
                ).format(table=rv_table),
                (f"rv_{ULID()}", rec_id, cycle, result.engine_version, result.verdict, metric_key,
                 result.baseline_value, result.eval_value, result.personal_sigma,
                 json.dumps(result.coverage), json.dumps(result.rule_trace)),
            )
        conn.commit()
    return EvaluateResponse(evaluated=True, verdict=result.verdict)


class ActionLoop(BaseModel):
    issued: str
    title: str
    metric: str
    metric_label: Optional[str]
    unit: Optional[str]
    before: Optional[float]
    after: Optional[float]
    delta_abs: Optional[float]
    judgment: str
    status: str
    expect: str


_STATUS_TEXT = {"effective": "сработало", "adverse": "стало хуже", "no_effect": "без изменений",
                "partial": "частично сработало", "data_gap": "не хватило данных"}
_JUDGMENT_MAP = {"effective": "good", "partial": "good", "adverse": "bad", "no_effect": "neutral", "data_gap": "neutral"}


def get_loops(limit: int = 3) -> list[ActionLoop]:
    """Замена прозе-парсеру в Build Health JSON: то же самое, что дашборд получал
    раньше, но из committed rv_/rc_/ex_, не из перепарсивания Recommendation_Text
    при каждом вызове."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT rc.started_ts, rc.title, rc.rationale, ex.metric_key, ex.metric_label, ex.unit, "
                "rv.baseline_value, rv.eval_value, rv.verdict "
                "FROM {rv} rv "
                "JOIN {rc} rc ON rc.id = rv.rec_id "
                "JOIN {ex} ex ON ex.rec_id = rv.rec_id AND ex.role = 'primary' "
                "WHERE rv.status = 'current' AND rv.verdict != 'data_gap' "
                "ORDER BY rc.started_ts DESC LIMIT %s"
            ).format(rv=sql.Identifier(schema(), "recommendation_verdict"),
                     rc=sql.Identifier(schema(), "recommendation"),
                     ex=sql.Identifier(schema(), "expectation")),
            (limit,),
        )
        rows = cur.fetchall()

    loops = []
    for started_ts, title, rationale, metric_key, metric_label, unit, before, after, verdict in rows:
        delta = (float(after) - float(before)) if (before is not None and after is not None) else None
        loops.append(ActionLoop(
            issued=started_ts.date().isoformat(), title=title, metric=metric_key,
            metric_label=metric_label, unit=unit,
            before=float(before) if before is not None else None,
            after=float(after) if after is not None else None,
            delta_abs=delta, judgment=_JUDGMENT_MAP.get(verdict, "neutral"),
            status=_STATUS_TEXT.get(verdict, verdict), expect=rationale or "",
        ))
    return loops
