"""
Транзакционная запись StagedWrite (план §3.6, Phase 5): health.* + card.* в
ОДНОЙ транзакции — либо всё, либо ничего. Заменяет 21-узловой дуальный
write-путь старого доктора (§0: "падение на любом из 21 узла оставляет ход
записанным наполовину") одной функцией с одним COMMIT/ROLLBACK.

Инварианты, перенесённые из промпта в код:
- одновременно только ОДНО открытое расследование (health.investigations,
  status='open') — Open_Investigation отклоняется, если уже есть открытое,
  а не полагается на то, догадалась ли модель вызвать Read_Investigations;
- kind вне контракта — отказ (сюда попадают только валидные StagedWrite,
  tools.py уже проверил форму аргументов pydantic-схемой — детерминированные
  ворота ДО исполнения инструмента, план §3.4);
- идемпотентность по turn_id: card.journal уже несёт "кто/когда" каждой
  card.*-записи (reason=f"source={turn_id}") — перед записью проверяем, не
  закоммичен ли уже этот turn_id, повтор коммита — no-op, не дубль.

health.symptom_log/doctor_notes — append-only лог (как и было у старого
доктора: каждое упоминание — новая строка с своим ts, не мутация текущего
состояния) — Read_Symptoms/Analyze_Symptom_Food строят историю по этим
строкам. card.episode/fact — через уже готовые write_path.apply_draft() +
extraction.Draft, чтобы П4-память видела ту же картину, не отдельную копию.
"""
import os
from typing import Optional

from psycopg import sql
from ulid import ULID

from app import timeutil
from app.db import get_conn, schema
from app.doctor.contract import (
    CloseInvestigationArgs, CloseRecommendationArgs, DisposeAnomalyArgs, OpenInvestigationArgs, PlanLabArgs,
    RecordNoteArgs, RecordSymptomArgs, StagedWrite, UpdateInvestigationArgs,
)
from app.extraction import Draft
from app.journal import write_journal
from app.write_path import apply_draft

# 2026-09-21 (#38/#47, аудит ZCode "тесты пишут в боевую health.*"): тот же
# переключатель, что уже использует app/registrar.py для health.visits/results —
# tests/conftest.py задаёт card_test, изолируя тесты commit.py от РЕАЛЬНОЙ
# медкарты. Найдено этим же фиксом: 5 "известных" падений test_doctor_commit.py
# держались не из-за бага в коде, а потому что тесты писали Open_Investigation
# прямо в health.investigations — и там уже 2+ недели лежит настоящая открытая
# запись Влада (radikulopatiya-l5s1-right-leg), с которой тестовый инвариант
# "только одно открытое расследование" честно и предсказуемо конфликтовал.
_HEALTH_SCHEMA = os.environ.get("REGISTRAR_HEALTH_SCHEMA", "health")

# T1 (внешний аудит логики, 2026-09-22): все ДАТЫ здесь считаются в зоне
# человека через app/timeutil.py, НЕ через CURRENT_DATE — Postgres живёт в
# UTC, и до 10:00 по Владивостоку CURRENT_DATE давал ВЧЕРАШНЮЮ дату (живое
# доказательство: health.doctor_notes id 135/73). Моменты событий (now())
# не трогаем — они и должны быть UTC.


class CommitError(Exception):
    """Инвариант нарушен (второе открытое расследование, неизвестный kind) —
    отказ, не тихая потеря (план §3.6: "не тихая потеря, а отказ")."""


def already_committed(cur, turn_id: str) -> bool:
    """Проверяет маркер, а не косвенные следствия (симптом мог отсутствовать
    в батче — note/lab_plan-only коммит не создаёт journal-запись через
    apply_draft) — маркер пишется явно и безусловно в apply_staged_writes()."""
    cur.execute(
        f"SELECT 1 FROM {schema()}.journal WHERE object_type = 'dialog_turn' "
        f"AND object_id = %s AND op = 'commit' LIMIT 1",
        (turn_id,),
    )
    return cur.fetchone() is not None


def _write_symptom(cur, args: RecordSymptomArgs, turn_id: str) -> None:
    cur.execute(
        f"INSERT INTO {_HEALTH_SCHEMA}.symptom_log "
        "(symptom_id, ts, symptom, system, severity, status, change, domain, context, hypothesis, notes) "
        "VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (args.symptom_id, args.symptom, args.system, args.severity, args.status,
         args.change, args.domain, args.context, args.hypothesis, args.notes),
    )
    draft = Draft(
        symptom_key=args.symptom_id, onset_expr=args.context, intensity=args.severity,
        triggers=[], negation=(args.status == "resolved"), closure=(args.status == "resolved"),
        confidence=0.9,
    )
    apply_draft(cur, draft, turn_id)


def _write_note(cur, args: RecordNoteArgs) -> None:
    cur.execute(
        f"INSERT INTO {_HEALTH_SCHEMA}.doctor_notes (note_date, category, note, trigger, plan) "
        "VALUES (%s, %s, %s, %s, %s)",
        (timeutil.today(), args.category, args.note, args.trigger, args.plan),
    )


def _has_open_investigation(cur, exclude_inv_id: Optional[str] = None) -> bool:
    if exclude_inv_id:
        cur.execute(
            f"SELECT 1 FROM {_HEALTH_SCHEMA}.investigations WHERE lower(status) = 'open' AND inv_id != %s LIMIT 1",
            (exclude_inv_id,),
        )
    else:
        cur.execute(f"SELECT 1 FROM {_HEALTH_SCHEMA}.investigations WHERE lower(status) = 'open' LIMIT 1")
    return cur.fetchone() is not None


def _open_investigation(cur, args: OpenInvestigationArgs) -> None:
    if _has_open_investigation(cur):
        raise CommitError(
            f"уже есть открытое расследование — Open_Investigation({args.inv_id}) отклонён "
            "(план §3.6: одновременно только одно)"
        )
    cur.execute(
        f"INSERT INTO {_HEALTH_SCHEMA}.investigations "
        "(inv_id, opened, updated, status, trigger, trigger_detail, hypothesis) "
        "VALUES (%s, %s, %s, 'open', %s, %s, %s) "
        "ON CONFLICT (inv_id) DO NOTHING",
        (args.inv_id, timeutil.today(), timeutil.today(), args.trigger, args.trigger_detail, args.hypothesis),
    )


def _update_investigation(cur, args: UpdateInvestigationArgs) -> None:
    cur.execute(
        f"UPDATE {_HEALTH_SCHEMA}.investigations SET updated = %s, "
        "hypothesis = COALESCE(%s, hypothesis), findings = COALESCE(%s, findings), "
        "questions_pending = COALESCE(%s, questions_pending), "
        "labs_suggested = COALESCE(%s, labs_suggested) "
        "WHERE inv_id = %s AND lower(status) = 'open'",
        (timeutil.today(), args.hypothesis, args.findings, args.questions_pending, args.labs_suggested, args.inv_id),
    )
    if cur.rowcount == 0:
        raise CommitError(f"Update_Investigation({args.inv_id}): нет открытого расследования с этим inv_id")


def _close_investigation(cur, args: CloseInvestigationArgs) -> None:
    cur.execute(
        f"UPDATE {_HEALTH_SCHEMA}.investigations SET status = 'report_ready', updated = %s, "
        "closed = %s, findings = COALESCE(%s, findings), "
        "doctor_brief = COALESCE(%s, doctor_brief), referral = COALESCE(%s, referral) "
        "WHERE inv_id = %s AND lower(status) = 'open'",
        (timeutil.today(), timeutil.today(), args.findings, args.doctor_brief, args.referral, args.inv_id),
    )
    if cur.rowcount == 0:
        raise CommitError(f"Close_Investigation({args.inv_id}): нет открытого расследования с этим inv_id")


def _plan_lab(cur, args: PlanLabArgs) -> None:
    plan_id = f"LP-{ULID()}"
    next_due = None
    if args.interval_months:
        cur.execute("SELECT to_char(%s::date + (%s || ' months')::interval, 'YYYY-MM-DD')",
                    (timeutil.today(), args.interval_months))
        next_due = cur.fetchone()[0]
    cur.execute(
        f'INSERT INTO {_HEALTH_SCHEMA}.lab_plan '
        '("Plan_ID", "Test", "Category", "Interval_Months", "Next_Due", "Reason", "Status", "Source") '
        "VALUES (%s, %s, %s, %s, %s, %s, 'active', 'AI-доктор')",
        (plan_id, args.test, args.category,
         str(args.interval_months) if args.interval_months else None, next_due, args.reason),
    )


def _close_recommendation(cur, args: CloseRecommendationArgs) -> None:
    """«Петля исходов» (2026-09-24, часть 4) — единственное исключение из «не
    трогать app/doctor/». card.recommendation, не health.* — schema(), не
    _HEALTH_SCHEMA. Поиск по подстроке title, не по id (см. CloseRecommendationArgs)."""
    from app.recommendations import close_recommendation

    q = sql.SQL("SELECT id, title FROM {t} WHERE status = 'active' AND title ILIKE %s") \
        .format(t=sql.Identifier(schema(), "recommendation"))
    cur.execute(q, (f"%{args.title}%",))
    rows = cur.fetchall()
    if not rows:
        raise CommitError(f"Close_Recommendation({args.title!r}): активных рекомендаций с таким названием не найдено")
    if len(rows) > 1:
        titles = ", ".join(r[1] for r in rows)
        raise CommitError(f"Close_Recommendation({args.title!r}): совпадений несколько ({titles}) — уточни формулировку")
    rec_id = rows[0][0]
    if not close_recommendation(cur, rec_id, args.reason or "closed_by_doctor_chat"):
        raise CommitError(f"Close_Recommendation({args.title!r}): не удалось закрыть {rec_id}")


def _dispose_anomaly(cur, args: DisposeAnomalyArgs) -> None:
    """«Мост аномалия -> действие» (2026-09-25) — единственное исключение из
    «не трогать app/doctor/» в этом тикете, вместе с досье-блоком истории
    диспозиций (context.py). Логика самого моста — app/anomaly_disposition.py,
    здесь только адаптация StagedWrite -> её сигнатура."""
    from app.anomaly_disposition import dispose

    hypotheses = [h.model_dump() for h in args.hypotheses] if args.hypotheses else None
    result = dispose(cur, args.metric, args.disposition, reason=args.reason,
                      window_days=args.window_days, hypotheses=hypotheses)
    if not result["ok"]:
        raise CommitError(f"Dispose_Anomaly({args.metric!r}): {result['error']}")


_HANDLERS = {
    "symptom": (RecordSymptomArgs, _write_symptom),
    "note": (RecordNoteArgs, _write_note),
    "investigation_open": (OpenInvestigationArgs, _open_investigation),
    "investigation_update": (UpdateInvestigationArgs, _update_investigation),
    "investigation_close": (CloseInvestigationArgs, _close_investigation),
    "lab_plan": (PlanLabArgs, _plan_lab),
    "recommendation_close": (CloseRecommendationArgs, _close_recommendation),
    "anomaly_dispose": (DisposeAnomalyArgs, _dispose_anomaly),
}


def apply_staged_writes(staged_writes: list[StagedWrite], turn_id: str) -> dict:
    """Одна транзакция для всего списка — план §3.6 "всё либо ничего". Пустой
    список — no-op (не открывает транзакцию впустую)."""
    if not staged_writes:
        return {"committed": False, "reason": "nothing_to_write"}

    with get_conn() as conn:
        with conn.cursor() as cur:
            if already_committed(cur, turn_id):
                return {"committed": False, "reason": "already_committed"}

            applied = []
            for w in staged_writes:
                entry = _HANDLERS.get(w.kind)
                if entry is None:
                    raise CommitError(f"неизвестный kind в StagedWrite: {w.kind}")
                model_cls, handler = entry
                if w.kind == "symptom":
                    handler(cur, model_cls(**w.payload), turn_id)
                else:
                    handler(cur, model_cls(**w.payload))
                applied.append(w.kind)

            # Маркер идемпотентности — безусловно, независимо от того, что
            # именно было в батче (note/lab_plan-only коммит иначе не оставил
            # бы никакого проверяемого следа, см. already_committed()).
            write_journal(cur, "dialog_turn", turn_id, "commit",
                          diff={"applied": applied}, reason=f"source={turn_id}")
        conn.commit()
    return {"committed": True, "applied": applied}
