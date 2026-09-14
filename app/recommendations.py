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
from app.memory import create_clinical_note
from app.gates import (
    GateFailure,
    gate1_sanity,
    gate2_measurability,
    gate3_interaction,
    gate4_gate_compat,
    gate5_dedup,
    gate6_priority,
)
from app.journal import write_journal
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


def sync_recommendation(req: RecommendationSyncRequest, priority: Optional[str] = None) -> RecommendationSyncResponse:
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
                    "INSERT INTO {table} (id, ts_event, provenance, verification, title, action, rationale, kind, status, started_ts, cycle, priority) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, 'active', %s, 1, %s) "
                    "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
                ).format(table=table),
                (new_id, req.started_ts, provenance, req.title, req.action, req.rationale, req.kind, req.started_ts, priority),
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
            else:
                write_journal(cur, "recommendation", rc_id, "create",
                              diff={"title": req.title, "action": req.action, "rationale": req.rationale,
                                    "kind": req.kind, "started_ts": str(req.started_ts), "source_ref": req.source_ref,
                                    "origin": req.origin},
                              link_back=True)

            measurable = bool(req.metric_key and req.direction and req.magnitude is not None)
            if created and measurable:
                ex_id = f"ex_{ULID()}"
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {table} (id, rec_id, cycle, metric_key, metric_label, unit, type, direction, magnitude, window_days, lag_days, baseline_days, role) "
                        "VALUES (%s, %s, 1, %s, %s, %s, 'delta_abs', %s, %s, %s, %s, %s, 'primary')"
                    ).format(table=ex_table),
                    (ex_id, rc_id, req.metric_key, req.metric_label, req.unit,
                     req.direction, req.magnitude, req.window_days, req.lag_days, req.baseline_days),
                )
                # expectation не имеет колонки journal_ref (не входит в _HAS_JOURNAL_REF) —
                # пишем запись журнала без обратной ссылки, сама запись всё равно находима.
                write_journal(cur, "expectation", ex_id, "create",
                              diff={"rec_id": rc_id, "metric_key": req.metric_key, "direction": req.direction,
                                    "magnitude": req.magnitude, "window_days": req.window_days,
                                    "lag_days": req.lag_days, "baseline_days": req.baseline_days})
        conn.commit()
    return RecommendationSyncResponse(id=rc_id, created=created, measurable=measurable)


class ProposeRequest(RecommendationSyncRequest):
    """У советника нет привилегированного пути записи (П3 §2.1) — этот же черновик,
    только теперь проходит G1-G6 ДО того как стать rc_. is_bioage_driver/metric_overdue
    считает вызывающий (Weekly Advisor) — card-service физически не имеет доступа к
    схеме health, где живут PhenoAge-драйверы и график лабораторных пересдач."""
    is_bioage_driver: bool = False
    metric_overdue: bool = False


class ProposeResponse(BaseModel):
    accepted: bool
    id: Optional[str] = None
    measurable: Optional[bool] = None
    priority: Optional[str] = None
    duplicate_of: Optional[str] = None
    rejected_gate: Optional[str] = None
    rejected_reason: Optional[str] = None


def propose_recommendation(req: ProposeRequest) -> ProposeResponse:
    """Ворота G1-G6, ДЕТЕРМИНИРОВАННО, ДО записи rc_ (П3 §2.1-2.2). Ни один провал
    G1/G3/G4 не создаёт объект — советник получает структурированный отказ, не пишет
    прозу напрямую в чат в обход этого пути (Gap 2, CARD_ARCHITECTURE_PLAN §5)."""
    text = " ".join(filter(None, [req.title, req.action, req.rationale]))

    g1 = gate1_sanity(req.metric_key, req.direction, req.magnitude, req.window_days, req.lag_days)
    if g1:
        return ProposeResponse(accepted=False, rejected_gate=g1.gate, rejected_reason=g1.reason)

    g3 = gate3_interaction(text)
    if g3:
        return ProposeResponse(accepted=False, rejected_gate=g3.gate, rejected_reason=g3.reason)

    with get_conn() as conn:
        with conn.cursor() as cur:
            g4 = gate4_gate_compat(cur, text)
            if g4:
                return ProposeResponse(accepted=False, rejected_gate=g4.gate, rejected_reason=g4.reason)

            g5 = gate5_dedup(cur, req.kind, req.action, req.metric_key, req.direction)
            if g5:
                return ProposeResponse(accepted=False, rejected_gate=g5.gate, rejected_reason=g5.reason,
                                        duplicate_of=g5.ref_id)

            measure_mode = gate2_measurability(cur, req.metric_key)  # никогда не блокирует

    priority = gate6_priority(req.is_bioage_driver, req.metric_overdue)

    sync_req = RecommendationSyncRequest(**req.model_dump(exclude={"is_bioage_driver", "metric_overdue"}))
    if measure_mode == "unmeasurable":
        # G2 деградация (б): пишем без ex_, даже если направление/величина были даны —
        # витрина честно покажет "не измеримо", а не притворится измеренной.
        sync_req = sync_req.model_copy(update={"metric_key": None, "direction": None, "magnitude": None})
    result = sync_recommendation(sync_req, priority=priority)

    return ProposeResponse(accepted=True, id=result.id, measurable=result.measurable, priority=priority)


class EvaluateResponse(BaseModel):
    evaluated: bool
    verdict: Optional[str] = None
    reason: Optional[str] = None


def evaluate_recommendation(rec_id: str) -> EvaluateResponse:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT started_ts, title FROM {table} WHERE id = %s")
                .format(table=sql.Identifier(schema(), "recommendation")),
                (rec_id,),
            )
            rc_row = cur.fetchone()
            if rc_row is None:
                return EvaluateResponse(evaluated=False, reason="recommendation не найдена")
            started_ts, rec_title = rc_row

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
                sql.SQL("UPDATE {table} SET status = 'superseded' WHERE rec_id = %s AND cycle = %s AND status = 'current' RETURNING id")
                .format(table=rv_table),
                (rec_id, cycle),
            )
            # recommendation_verdict тоже без journal_ref (не в _HAS_JOURNAL_REF) —
            # обратная ссылка не проставляется, запись в журнале всё равно есть.
            for (superseded_id,) in cur.fetchall():
                write_journal(cur, "recommendation_verdict", superseded_id, "update",
                              diff={"status": "superseded"}, reason=f"cycle={cycle} recompute")

            new_rv_id = f"rv_{ULID()}"
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, rec_id, cycle, engine_version, verdict, metric_key, baseline_value, eval_value, personal_sigma, coverage, rule_trace, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'current')"
                ).format(table=rv_table),
                (new_rv_id, rec_id, cycle, result.engine_version, result.verdict, metric_key,
                 result.baseline_value, result.eval_value, result.personal_sigma,
                 json.dumps(result.coverage), json.dumps(result.rule_trace)),
            )
            write_journal(cur, "recommendation_verdict", new_rv_id, "create",
                          diff={"rec_id": rec_id, "cycle": cycle, "verdict": result.verdict,
                                "engine_version": result.engine_version, "baseline_value": result.baseline_value,
                                "eval_value": result.eval_value})

            # П4 §2.1: clinical mn_ создаётся автоматически при no_effect/adverse —
            # "то, что живой врач помнит о пациенте, не перечитывая карту".
            create_clinical_note(cur, rec_id, rec_title, result.verdict, metric_key)
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
