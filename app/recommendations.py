"""
Phase 3 — рекомендации как объекты (П3). Закрывает находку №1: рекомендация теперь
строка с id, а не JSON-блок, перепарсиваемый из текста при каждом пересчёте кэша.

Три операции:
- sync: создать/дедуп rc_ (+ex_, если measurable) по source_ref.
- evaluate: посчитать вердикт для одной rc_ через verdict_engine, записать rv_.
- loops: отдать дашборду то же самое, что раньше строил парсер прозы — но из rv_.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from psycopg import sql
from pydantic import BaseModel
from ulid import ULID

from app import run_log
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
    gate7_expectation_required,
)
from app.journal import write_journal
from app.scheduler_alert import alert_on_failure
from app.verdict_engine import Expectation, Fact, evaluate as run_verdict_engine

logger = logging.getLogger(__name__)


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
    # «Петля исходов» (2026-09-24, G7): expectation_type — delta_abs (по умолчанию,
    # обратная совместимость) | threshold | frequency. unmeasurable_reason — G7
    # принимает черновик БЕЗ metric_key только если эта причина названа явно.
    # freq_min_ratio — только для type='frequency' (доля дней окна, см. verdict_engine).
    expectation_type: str = "delta_abs"
    unmeasurable_reason: Optional[str] = None
    freq_min_ratio: Optional[float] = None
    publication_id: Optional[str] = None  # «научный контур» 2026-09-25 — трассировка "откуда идея"


class RecommendationSyncResponse(BaseModel):
    id: str
    created: bool
    measurable: bool


# «Петля исходов» (2026-09-24, часть 4): topic_key группирует последовательные
# ревизии одного и того же совета ("снизить жиры" 06.09 -> "жиры до 27г" 13.09 ->
# "жиры до 28г" 20.09 — три рекомендации, одна тема) для supersede-цепочки при
# создании. Тот же MVP-уровень, что CONTRA_SYNONYMS в gates.py — растущий словарь
# по факту столкновений, не NLP. "Минимальный ключ" по спеке тикета — если ни
# один паттерн не совпал, ключ строится из kind (если есть) или первых слов title.
_TOPIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("diet_saturated_fat", ["насыщенных жир", "насыщенные жир", "жиров до", "ограничение жиров"]),
    ("diet_protein", ["белок", "белка "]),
    ("steps_stability", ["суточный шаг", "шагов"]),
    ("ortho_l5s1", ["l5/s1", "l5-s1", "ортопедическ"]),
    ("ortho_elbow", ["локт"]),
    ("cardio_referral", ["кардиолог", "apob", "lp(a)"]),
    ("neuro_referral", ["невролог", "онемени"]),
]


def _derive_topic_key(title: Optional[str], action: Optional[str], kind: Optional[str],
                       metric_key: Optional[str]) -> Optional[str]:
    text = f"{title or ''} {action or ''}".lower()
    for key, needles in _TOPIC_PATTERNS:
        if any(n in text for n in needles):
            return key
    if metric_key:
        return f"metric_{metric_key}"
    if kind:
        return f"kind_{kind}"
    words = [w for w in text.split() if len(w) >= 4][:3]
    return f"title_{'_'.join(words)}" if words else None


def _supersede_same_topic(cur, new_id: str, topic_key: Optional[str]) -> None:
    """При создании новой rc_ с тем же topic_key — предыдущие активные ревизии
    той же темы становятся superseded (не closed: closed зарезервирован за
    исходами вердикта — no_effect/adverse/not_adhered, см. evaluate_recommendation).
    Тихой перезаписи данных нет — старая строка остаётся, просто status+ссылка."""
    if not topic_key:
        return
    table = sql.Identifier(schema(), "recommendation")
    cur.execute(
        sql.SQL("SELECT id FROM {table} WHERE status = 'active' AND topic_key = %s AND id != %s")
        .format(table=table),
        (topic_key, new_id),
    )
    for (old_id,) in cur.fetchall():
        cur.execute(
            sql.SQL("UPDATE {table} SET status = 'superseded', superseded_by = %s, "
                    "stop_reason = 'superseded_by_topic' WHERE id = %s").format(table=table),
            (new_id, old_id),
        )
        write_journal(cur, "recommendation", old_id, "update",
                      diff={"status": "superseded", "superseded_by": new_id},
                      reason=f"topic_key={topic_key}: новая рекомендация {new_id} по той же теме")


def sync_recommendation(req: RecommendationSyncRequest, priority: Optional[str] = None) -> RecommendationSyncResponse:
    table = sql.Identifier(schema(), "recommendation")
    ex_table = sql.Identifier(schema(), "expectation")
    new_id = f"rc_{ULID()}"
    provenance = json.dumps({
        "origin": req.origin, "source_id": None, "extraction": None,
        "model": None, "prompt_version": None, "source_ref": req.source_ref,
    })
    topic_key = _derive_topic_key(req.title, req.action, req.kind, req.metric_key)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, ts_event, provenance, verification, title, action, rationale, kind, status, started_ts, cycle, priority, topic_key, publication_id) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, 'active', %s, 1, %s, %s, %s) "
                    "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
                ).format(table=table),
                (new_id, req.started_ts, provenance, req.title, req.action, req.rationale, req.kind,
                 req.started_ts, priority, topic_key, req.publication_id),
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
                                    "origin": req.origin, "topic_key": topic_key},
                              link_back=True)
                _supersede_same_topic(cur, rc_id, topic_key)

            measurable = bool(req.metric_key and req.direction and req.magnitude is not None)
            if created:
                ex_id = f"ex_{ULID()}"
                if measurable:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {table} (id, rec_id, cycle, metric_key, metric_label, unit, type, direction, magnitude, window_days, lag_days, baseline_days, freq_min_ratio, role) "
                            "VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'primary')"
                        ).format(table=ex_table),
                        (ex_id, rc_id, req.metric_key, req.metric_label, req.unit, req.expectation_type,
                         req.direction, req.magnitude, req.window_days, req.lag_days, req.baseline_days,
                         req.freq_min_ratio),
                    )
                    diff = {"rec_id": rc_id, "metric_key": req.metric_key, "type": req.expectation_type,
                            "direction": req.direction, "magnitude": req.magnitude, "window_days": req.window_days,
                            "lag_days": req.lag_days, "baseline_days": req.baseline_days}
                else:
                    # G7 (gates.py) — до этой точки доходит только если caller либо назвал
                    # unmeasurable_reason, либо (запасной случай — прямой вызов sync_recommendation
                    # в обход propose_recommendation) не назвал вовсе. Второе всё равно получает
                    # строку ex_, а не тишину — инвариант "у активной рекомендации всегда есть
                    # ровно одна primary ex_" держится независимо от пути создания.
                    reason = req.unmeasurable_reason or "причина не указана вызывающим (создано в обход propose_recommendation)"
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {table} (id, rec_id, cycle, metric_key, type, reason, role) "
                            "VALUES (%s, %s, 1, NULL, 'unmeasurable', %s, 'primary')"
                        ).format(table=ex_table),
                        (ex_id, rc_id, reason),
                    )
                    diff = {"rec_id": rc_id, "type": "unmeasurable", "reason": reason}
                # expectation не имеет колонки journal_ref (не входит в _HAS_JOURNAL_REF) —
                # пишем запись журнала без обратной ссылки, сама запись всё равно находима.
                write_journal(cur, "expectation", ex_id, "create", diff=diff)
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


def _log_rejected_draft(cur, req: "ProposeRequest", gate: str, reason: str) -> None:
    """«Петля исходов» часть 1: отклонённый ворoтами черновик пишется в issue_log,
    не пропадает молча — тот же принцип, что и у «петли самоулучшения» (issue_log.py,
    2026-09-23): находка должна оставлять след, даже если ничего не создано."""
    from app import issue_log
    issue_log.record_issue(
        cur, f"gate_reject:{req.source_ref}", source="propose_recommendation",
        summary=f"{gate}: {reason} — черновик «{req.title}» отклонён",
    )


def propose_recommendation(req: ProposeRequest) -> ProposeResponse:
    """Ворота G1-G7, ДЕТЕРМИНИРОВАННО, ДО записи rc_ (П3 §2.1-2.2, + «петля
    исходов» 2026-09-24). Ни один провал не создаёт объект — советник получает
    структурированный отказ, не пишет прозу напрямую в чат в обход этого пути
    (Gap 2, CARD_ARCHITECTURE_PLAN §5). Отклонённые черновики логируются
    (issue_log), не пропадают тихо."""
    text = " ".join(filter(None, [req.title, req.action, req.rationale]))

    with get_conn() as conn:
        with conn.cursor() as cur:
            g1 = gate1_sanity(req.metric_key, req.direction, req.magnitude, req.window_days, req.lag_days)
            if g1:
                _log_rejected_draft(cur, req, g1.gate, g1.reason)
                conn.commit()
                return ProposeResponse(accepted=False, rejected_gate=g1.gate, rejected_reason=g1.reason)

            g7 = gate7_expectation_required(req.metric_key, req.direction, req.magnitude, req.unmeasurable_reason)
            if g7:
                _log_rejected_draft(cur, req, g7.gate, g7.reason)
                conn.commit()
                return ProposeResponse(accepted=False, rejected_gate=g7.gate, rejected_reason=g7.reason)

            g3 = gate3_interaction(text)
            if g3:
                _log_rejected_draft(cur, req, g3.gate, g3.reason)
                conn.commit()
                return ProposeResponse(accepted=False, rejected_gate=g3.gate, rejected_reason=g3.reason)

            g4 = gate4_gate_compat(cur, text)
            if g4:
                _log_rejected_draft(cur, req, g4.gate, g4.reason)
                conn.commit()
                return ProposeResponse(accepted=False, rejected_gate=g4.gate, rejected_reason=g4.reason)

            g5 = gate5_dedup(cur, req.kind, req.action, req.metric_key, req.direction)
            if g5:
                _log_rejected_draft(cur, req, g5.gate, g5.reason)
                conn.commit()
                return ProposeResponse(accepted=False, rejected_gate=g5.gate, rejected_reason=g5.reason,
                                        duplicate_of=g5.ref_id)

            measure_mode = gate2_measurability(cur, req.metric_key)  # никогда не блокирует
            # ни один из G1-G5/G7 не отклонил — весь блок был чтением (SELECT),
            # commit() не нужен, но conn закроется штатно на выходе из with.

    priority = gate6_priority(req.is_bioage_driver, req.metric_overdue)

    sync_req = RecommendationSyncRequest(**req.model_dump(exclude={"is_bioage_driver", "metric_overdue"}))
    if measure_mode == "unmeasurable" and req.metric_key:
        # G2 деградация (б): metric_key объявлен, но не входит в metric_coverage —
        # технически неизмеримо ДАЖЕ если советник дал direction+magnitude. Раньше
        # это просто теряло ex_ целиком (см. sync_recommendation до 2026-09-24);
        # теперь G7 гарантирует причину — если советник её не назвал, называем сами,
        # честно указывая, что дело в отсутствии покрытия метрики, а не в решении советника.
        reason = req.unmeasurable_reason or (
            f"metric_key={req.metric_key!r} не входит в metric_coverage — неизмеримо технически, не по решению советника"
        )
        sync_req = sync_req.model_copy(update={
            "metric_key": None, "direction": None, "magnitude": None, "unmeasurable_reason": reason,
        })
    result = sync_recommendation(sync_req, priority=priority)

    return ProposeResponse(accepted=True, id=result.id, measurable=result.measurable, priority=priority)


class EvaluateResponse(BaseModel):
    evaluated: bool
    verdict: Optional[str] = None
    reason: Optional[str] = None


# Вердикты, при которых рекомендация закрывается (часть 4): "интервенция была
# опробована и либо не сработала, либо, судя по частоте, не выполнялась" — оба
# случая просят пересмотр, не молчаливое дальнейшее висение в активных.
# effective/partial остаются активными (работает, продолжаем измерять цикл 2+);
# data_gap — не вердикт-исход, просто "рано считать", тоже остаётся активной.
_CLOSING_VERDICTS = {"no_effect", "adverse", "not_adhered"}


def _find_confounders(cur, rec_id: str, topic_key: Optional[str], window_from: datetime, window_to: datetime) -> list[str]:
    """Часть 5: другие активные рекомендации/вмешательства, чьё время действия
    пересекается с окном оценки ЭТОЙ рекомендации — возможные конфаундеры вывода.
    Та же тема (topic_key) исключается — последовательные ревизии одного совета
    не конфаундеры друг друга, это одна и та же интервенция. Сезонных baseline'ов
    здесь сознательно нет (см. докстринг verdict_engine.py) — только пересечение
    по времени, простое и честное про свои пределы."""
    names: list[str] = []
    rec_q = sql.SQL(
        "SELECT title FROM {t} WHERE status = 'active' AND id != %s "
        "AND (topic_key IS NULL OR topic_key != %s) "
        "AND started_ts IS NOT NULL AND started_ts < %s"
    ).format(t=sql.Identifier(schema(), "recommendation"))
    cur.execute(rec_q, (rec_id, topic_key or "", window_to))
    names.extend(row[0] for row in cur.fetchall())

    iv_q = sql.SQL(
        "SELECT name FROM {t} WHERE status = 'active' "
        "AND started_ts IS NOT NULL AND started_ts < %s "
        "AND (ended_ts IS NULL OR ended_ts > %s)"
    ).format(t=sql.Identifier(schema(), "intervention"))
    cur.execute(iv_q, (window_to, window_from))
    names.extend(row[0] for row in cur.fetchall())
    return names


def evaluate_recommendation(rec_id: str) -> EvaluateResponse:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT started_ts, title, topic_key FROM {table} WHERE id = %s")
                .format(table=sql.Identifier(schema(), "recommendation")),
                (rec_id,),
            )
            rc_row = cur.fetchone()
            if rc_row is None:
                return EvaluateResponse(evaluated=False, reason="recommendation не найдена")
            started_ts, rec_title, topic_key = rc_row

            cur.execute(
                sql.SQL(
                    "SELECT id, cycle, metric_key, metric_label, unit, type, direction, magnitude, "
                    "window_days, lag_days, baseline_days, freq_min_ratio "
                    "FROM {table} WHERE rec_id = %s AND role = 'primary' ORDER BY created_ts DESC LIMIT 1"
                ).format(table=sql.Identifier(schema(), "expectation")),
                (rec_id,),
            )
            ex_row = cur.fetchone()
            if ex_row is None:
                return EvaluateResponse(evaluated=False, reason="нет primary expectation — рекомендация не измерима")

            (ex_id, cycle, metric_key, metric_label, unit, ex_type, direction, magnitude,
             window_days, lag_days, baseline_days, freq_min_ratio) = ex_row

            if ex_type == "unmeasurable" or metric_key is None:
                # G7-путь (gates.py): явно неизмеримо, не "забыли задать ожидание" —
                # вердикт здесь принципиально не считается, не data_gap (data_gap
                # значит "могли бы посчитать, не хватило данных").
                return EvaluateResponse(evaluated=False, reason="рекомендация не измерима (explicit unmeasurable) — вердикт не применим")

            ex = Expectation(metric_key=metric_key, type=ex_type, direction=direction, magnitude=float(magnitude),
                              window_days=window_days, lag_days=lag_days, baseline_days=baseline_days,
                              freq_min_ratio=float(freq_min_ratio) if freq_min_ratio is not None else None)

            fact_from = started_ts - timedelta(days=max(baseline_days or 0, 90))
            fact_to = started_ts + timedelta(days=lag_days + window_days)
            cur.execute(
                sql.SQL("SELECT ts_event, value_num FROM {table} WHERE metric_key = %s AND ts_event >= %s AND ts_event < %s AND value_num IS NOT NULL ORDER BY ts_event")
                .format(table=sql.Identifier(schema(), "fact")),
                (metric_key, fact_from, fact_to),
            )
            facts = [Fact(ts_event=t, value_num=float(v)) for t, v in cur.fetchall()]

            eval_window_from = started_ts + timedelta(days=lag_days)
            eval_window_to = eval_window_from + timedelta(days=window_days)
            confounders = _find_confounders(cur, rec_id, topic_key, eval_window_from, eval_window_to)

            result = run_verdict_engine(started_ts, ex, facts, confounders=confounders)

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
                    "INSERT INTO {table} (id, rec_id, cycle, engine_version, verdict, metric_key, baseline_value, eval_value, personal_sigma, coverage, adherence_pct, confounded, rule_trace, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'current')"
                ).format(table=rv_table),
                (new_rv_id, rec_id, cycle, result.engine_version, result.verdict, metric_key,
                 result.baseline_value, result.eval_value, result.personal_sigma,
                 json.dumps(result.coverage), result.adherence_pct, json.dumps(result.confounded),
                 json.dumps(result.rule_trace)),
            )
            write_journal(cur, "recommendation_verdict", new_rv_id, "create",
                          diff={"rec_id": rec_id, "cycle": cycle, "verdict": result.verdict,
                                "engine_version": result.engine_version, "baseline_value": result.baseline_value,
                                "eval_value": result.eval_value, "confounded": result.confounded})

            # П4 §2.1: clinical mn_ создаётся автоматически при no_effect/adverse/
            # not_adhered — "то, что живой врач помнит о пациенте, не перечитывая карту".
            create_clinical_note(cur, rec_id, rec_title, result.verdict, metric_key)

            # Часть 4: закрываем рекомендацию при исходе, который просит пересмотра
            # (не молчаливое дальнейшее висение в активных) — effective/partial/data_gap
            # остаются активными.
            if result.verdict in _CLOSING_VERDICTS:
                rec_table = sql.Identifier(schema(), "recommendation")
                cur.execute(
                    sql.SQL("UPDATE {table} SET status = 'closed', stop_reason = %s WHERE id = %s AND status = 'active'")
                    .format(table=rec_table),
                    (result.verdict, rec_id),
                )
                write_journal(cur, "recommendation", rec_id, "update",
                              diff={"status": "closed", "stop_reason": result.verdict},
                              reason=f"verdict={result.verdict} (cycle={cycle})")
        conn.commit()
    return EvaluateResponse(evaluated=True, verdict=result.verdict)


def close_recommendation(cur, rec_id: str, reason: Optional[str] = None) -> bool:
    """Ручное закрытие (часть 4) — единственный путь, которым доктор трогает
    app/recommendations.py (новый инструмент Close_Recommendation, app/doctor/
    tools.py+commit.py). Принимает ГОТОВЫЙ курсор, не открывает своё соединение —
    вызывается изнутри чужой транзакции (commit.py::apply_staged_writes, «всё
    либо ничего»). Возвращает False, если рекомендации с таким id нет или она
    уже не активна — вызывающий решает, считать ли это ошибкой."""
    table = sql.Identifier(schema(), "recommendation")
    cur.execute(
        sql.SQL("UPDATE {table} SET status = 'closed', stop_reason = %s WHERE id = %s AND status = 'active' RETURNING id")
        .format(table=table),
        (reason or "closed_manually", rec_id),
    )
    row = cur.fetchone()
    if row is None:
        return False
    write_journal(cur, "recommendation", rec_id, "update",
                  diff={"status": "closed", "stop_reason": reason or "closed_manually"},
                  reason="Close_Recommendation (доктор)")
    return True


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


_DIRECTION_ARROW = {"up": "↑", "down": "↓"}


class ActiveRecommendationExpectation(BaseModel):
    """«Петля исходов» (2026-09-24, часть 6, акс. критерий "дашборд показывает
    ожидание-или-неизмеримо для каждой активной рекомендации"): в отличие от
    get_loops() (только те, для кого УЖЕ посчитан не-data_gap вердикт), этот
    список — ВСЕ активные rc_, независимо от того, дошла ли очередь до оценки.
    G7 (gates.py) гарантирует, что ex_row здесь есть всегда — expectation
    отсутствующим не бывает, только измеримым или явно unmeasurable."""
    id: str
    title: str
    priority: Optional[str]
    is_unmeasurable: bool
    summary: str  # "ВСР ночью ↑6мс за 7дн" | "неизмеримо: <reason>"
    latest_verdict: Optional[str] = None


def get_active_recommendations() -> list[ActiveRecommendationExpectation]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT rc.id, rc.title, rc.priority, "
                "ex.type, ex.metric_label, ex.metric_key, ex.direction, ex.magnitude, ex.unit, ex.window_days, ex.reason, "
                "rv.verdict "
                "FROM {rc} rc "
                "JOIN {ex} ex ON ex.rec_id = rc.id AND ex.role = 'primary' AND ex.cycle = rc.cycle "
                "LEFT JOIN {rv} rv ON rv.rec_id = rc.id AND rv.status = 'current' "
                "WHERE rc.status = 'active' "
                "ORDER BY rc.started_ts DESC"
            ).format(rc=sql.Identifier(schema(), "recommendation"),
                     ex=sql.Identifier(schema(), "expectation"),
                     rv=sql.Identifier(schema(), "recommendation_verdict")),
        )
        rows = cur.fetchall()

    out = []
    for rec_id, title, priority, ex_type, metric_label, metric_key, direction, magnitude, unit, window_days, reason, verdict in rows:
        if ex_type == "unmeasurable" or metric_key is None:
            summary = f"неизмеримо: {reason or 'причина не указана'}"
            is_unmeasurable = True
        else:
            arrow = _DIRECTION_ARROW.get(direction, "")
            label = metric_label or metric_key
            mag = f"{magnitude:g}" if magnitude is not None else "?"
            unit_str = unit or ""
            window_str = f" за {window_days}дн" if window_days else ""
            summary = f"{label} {arrow}{mag}{unit_str}{window_str}"
            is_unmeasurable = False
        out.append(ActiveRecommendationExpectation(
            id=rec_id, title=title, priority=priority, is_unmeasurable=is_unmeasurable,
            summary=summary, latest_verdict=verdict,
        ))
    return out


# =====================================================================
# Автоматическая оценка (петля исходов, аудит логики 2026-09-23)
# =====================================================================
# НАХОДКА: весь движок (evaluate_recommendation/verdict_engine) был построен
# и даже имел собственный HTTP-эндпоинт (/recommendations/{id}/evaluate) —
# но НИЧТО его не вызывало само. Единственный способ получить вердикт был
# дёрнуть эндпоинт руками. За всё время (8 рекомендаций от Weekly Advisor,
# 1 измеримая) вердикт посчитан один раз, вручную, при отладке. "Петля
# исходов" не была мёртвой из-за отсутствия кода — она была мёртвой
# потому, что никто не нажимал на спусковой крючок. Теперь нажимает сам.

EVAL_INTERVAL_SECONDS = 24 * 3600  # раз в сутки — окна оценки день-гранулярные, чаще не нужно


def find_due_recommendations(cur) -> list[str]:
    """Измеримые активные рекомендации, чьё окно оценки (lag_days + window_days
    от started_ts) уже закрылось, и у которых ещё нет текущего НЕ-data_gap
    вердикта того же цикла — либо вердикта не было вовсе, либо в прошлый раз
    не хватило данных (data_gap) и стоит попробовать снова, вдруг подъехали."""
    cur.execute(
        sql.SQL(
            "SELECT rc.id FROM {rc} rc "
            "JOIN {ex} ex ON ex.rec_id = rc.id AND ex.role = 'primary' AND ex.cycle = rc.cycle "
            "WHERE rc.status = 'active' AND rc.started_ts IS NOT NULL "
            "AND ex.metric_key IS NOT NULL AND ex.type != 'unmeasurable' "
            "AND now() >= rc.started_ts + (COALESCE(ex.lag_days, 0) + COALESCE(ex.window_days, 7)) * interval '1 day' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM {rv} rv WHERE rv.rec_id = rc.id AND rv.cycle = rc.cycle "
            "  AND rv.status = 'current' AND rv.verdict != 'data_gap'"
            ")"
        ).format(rc=sql.Identifier(schema(), "recommendation"),
                 ex=sql.Identifier(schema(), "expectation"),
                 rv=sql.Identifier(schema(), "recommendation_verdict")),
    )
    return [r[0] for r in cur.fetchall()]


def run_once() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        due = find_due_recommendations(cur)
    ok = 0
    for rec_id in due:
        try:
            evaluate_recommendation(rec_id)
            ok += 1
        except Exception:
            logger.exception("recommendations: evaluate_recommendation упал для %s — попробуем на следующем тике", rec_id)
    if due:
        logger.info("recommendations: авто-оценка — %d/%d рекомендаций посчитано", ok, len(due))


def run_scheduler() -> None:
    logger.info("recommendations auto-evaluate scheduler: старт (каждые %d ч)", EVAL_INTERVAL_SECONDS // 3600)
    while True:
        try:
            run_once()
            run_log.mark_run("recommendations_evaluate")
        except Exception as e:
            logger.exception("recommendations: run_once упал целиком — повтор через обычный интервал")
            alert_on_failure("recommendations_evaluate", e)
        time.sleep(EVAL_INTERVAL_SECONDS)
