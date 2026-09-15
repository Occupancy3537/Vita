"""
card-service — «мозг» медицинской карты (П1–П8 из CARD_ARCHITECTURE_PLAN_2026-09-13.md).

Phase 0: /health + /ingest. /ingest делает РОВНО одну вещь — сохраняет сырьё в
source_message, до всякой обработки (П1 §1.1 "Сначала сырьё"). Никакого извлечения,
никакой валидации содержимого, никакого LLM здесь пока нет — это Phase 2 (write-path).

Сервис слушает только на 127.0.0.1 (см. README) — наружу торчит только n8n-webhook
за TLS, card-service публично не виден никогда.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Optional

import psycopg
from fastapi import Body, BackgroundTasks, FastAPI, HTTPException
from psycopg import sql
from pydantic import BaseModel
from ulid import ULID

from app.db import get_conn, schema
from app.doctor.intake import handle_update
from app.journal import write_journal
from app.memory import get_context, get_object, index_entity, run_pre_archive_check
from app.redflag_b import LayerBResult, classify as redflag_classify_b
from app.redflag_c import run_layer_c
from app.redflag_union import evaluate_and_record, record_rf_event
from app.recommendations import (
    ActionLoop,
    EvaluateResponse,
    ProposeRequest,
    ProposeResponse,
    RecommendationSyncRequest,
    RecommendationSyncResponse,
    evaluate_recommendation,
    get_loops,
    propose_recommendation,
    sync_recommendation,
)
from app.write_path import process as process_source

app = FastAPI(title="card-service", version="0.0.1")

Channel = Literal["telegram", "device", "lab", "visit", "manual"]


class IngestRequest(BaseModel):
    channel: Channel
    raw_text: str
    person_id: str = "self"
    ts_received: Optional[datetime] = None


class IngestResponse(BaseModel):
    id: str
    status: str
    duplicate: bool


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest) -> IngestResponse:
    if not req.raw_text or not req.raw_text.strip():
        raise HTTPException(status_code=422, detail="raw_text пуст")

    content_hash = hashlib.sha256(req.raw_text.encode("utf-8")).hexdigest()
    new_id = f"src_{ULID()}"
    ts = req.ts_received or datetime.now(timezone.utc)

    table = sql.Identifier(schema(), "source_message")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Дедуп по hash (П1 edge-кейс: повторный ingest того же сырья идемпотентен,
            # не ошибка и не дубль-строка).
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, person_id, channel, raw_text, ts_received, hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (hash) DO NOTHING "
                    "RETURNING id, status"
                ).format(table=table),
                (new_id, req.person_id, req.channel, req.raw_text, ts, content_hash),
            )
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return IngestResponse(id=row[0], status=row[1], duplicate=False)

            # Конфликт — уже есть строка с этим hash; вернуть её, не создавать новую.
            cur.execute(
                sql.SQL("SELECT id, status FROM {table} WHERE hash = %s").format(table=table),
                (content_hash,),
            )
            existing = cur.fetchone()
            conn.commit()
            if existing is None:
                # Не должно происходить (конфликт был, а строки нет) — гоним честную ошибку,
                # а не тихо теряем сырьё.
                raise HTTPException(status_code=500, detail="конфликт hash без найденной строки")
            return IngestResponse(id=existing[0], status=existing[1], duplicate=True)


class StructuredFact(BaseModel):
    metric_key: str
    value_num: float
    ts_event: datetime


class StructuredFactsRequest(BaseModel):
    facts: list[StructuredFact]
    person_id: str = "self"


class StructuredFactsResponse(BaseModel):
    written: int
    skipped_duplicate: int


def _write_structured_facts(facts: list[StructuredFact], origin: str) -> StructuredFactsResponse:
    """Прямой путь без LLM (П2 §3.8): числа из структурного источника -> fact,
    confirmed сразу — структурная ошибка невозможна, в отличие от текста. Дедуп по
    (metric_key, ts_event) в пределах origin — частичный уникальный индекс
    fact_<origin>_dedup, повторная отправка того же дня идемпотентна (ON CONFLICT
    DO NOTHING), не плодит дублей при повторных прогонах воркфлоу-источника."""
    table = sql.Identifier(schema(), "fact")
    written = 0
    skipped = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for f in facts:
                fact_id = f"f_{ULID()}"
                provenance = json.dumps({"origin": origin, "source_id": None, "extraction": None, "model": None, "prompt_version": None})
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {table} (id, ts_event, provenance, verification, metric_key, value_num) "
                        "VALUES (%s, %s, %s, 'confirmed', %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ).format(table=table),
                    (fact_id, f.ts_event, provenance, f.metric_key, f.value_num),
                )
                if cur.fetchone() is not None:
                    written += 1
                    write_journal(cur, "fact", fact_id, "create",
                                  diff={"metric_key": f.metric_key, "value_num": f.value_num,
                                        "ts_event": f.ts_event.isoformat(), "origin": origin},
                                  link_back=True)
                else:
                    skipped += 1
        conn.commit()
    return StructuredFactsResponse(written=written, skipped_duplicate=skipped)


@app.post("/facts/device", response_model=StructuredFactsResponse)
def facts_device(req: StructuredFactsRequest) -> StructuredFactsResponse:
    """Устройства (Garmin и т.п.) — см. _write_structured_facts. Дедуп-индекс:
    fact_device_dedup."""
    return _write_structured_facts(req.facts, origin="device")


@app.post("/facts/nutrition", response_model=StructuredFactsResponse)
def facts_nutrition(req: StructuredFactsRequest) -> StructuredFactsResponse:
    """Питание (day_sum — уже структурировано отдельным LLM-тегированием раньше в
    конвейере, здесь просто числа) — см. _write_structured_facts. Дедуп-индекс:
    fact_nutrition_dedup."""
    return _write_structured_facts(req.facts, origin="nutrition")


class InterventionSyncRequest(BaseModel):
    name: str
    source_ref: str  # стабильный внешний id (напр. recurringEventId календаря) — дедуп-ключ
    kind: Literal["drug", "supplement", "protocol", "behavior"] = "supplement"
    dose: Optional[str] = None
    regimen: Optional[str] = None
    started_ts: Optional[datetime] = None
    origin: str = "calendar"


class InterventionSyncResponse(BaseModel):
    id: str
    created: bool


@app.post("/interventions/sync", response_model=InterventionSyncResponse)
def interventions_sync(req: InterventionSyncRequest) -> InterventionSyncResponse:
    """Идемпотентная синхронизация intervention по внешнему source_ref (calendar
    recurringEventId и т.п.) — источник сказал о себе сам (user_direct-эквивалент:
    Влад сам завёл событие в своём календаре), поэтому verification='confirmed' сразу,
    без переспроса (W3-логика П2 §3.4, применённая к структурному источнику, не к тексту).
    Повторный вызов с тем же source_ref не создаёт вторую запись — только начальный
    синк создаёт объект; ведение статуса/дозы после создания — отдельная забота
    (ручная правка или будущий Phase-3-стиль пересмотр), не эта ручка."""
    table = sql.Identifier(schema(), "intervention")
    provenance = json.dumps({
        "origin": req.origin, "source_id": None, "extraction": None,
        "model": None, "prompt_version": None, "source_ref": req.source_ref,
    })
    new_id = f"iv_{ULID()}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, ts_event, provenance, verification, kind, name, dose, regimen, started_ts, status, prescriber) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, 'active', 'self') "
                    "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
                ).format(table=table),
                (new_id, req.started_ts or datetime.now(timezone.utc), provenance,
                 req.kind, req.name, req.dose, req.regimen, req.started_ts),
            )
            row = cur.fetchone()
            if row is not None:
                write_journal(cur, "intervention", row[0], "create",
                              diff={"name": req.name, "kind": req.kind, "dose": req.dose,
                                    "regimen": req.regimen, "started_ts": str(req.started_ts),
                                    "source_ref": req.source_ref, "origin": req.origin},
                              link_back=True)
                index_entity(cur, "substance", req.name.lower(), row[0], "intervention")
                conn.commit()
                return InterventionSyncResponse(id=row[0], created=True)

            cur.execute(
                sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s").format(table=table),
                (req.source_ref,),
            )
            existing = cur.fetchone()
            conn.commit()
            return InterventionSyncResponse(id=existing[0], created=False)


def _ensure_visit(cur, source_ref: str, title: Optional[str], raw_text: Optional[str], ts_event: datetime) -> str:
    """Идемпотентно по source_ref (внешний Visit_ID) — возвращает internal card.visit.id,
    создавая строку при первом обращении. Используется и напрямую (/visits/sync) и как
    побочный эффект /labs/result — документ с результатами может прийти раньше отдельного
    визит-синка, лаборатория не должна ждать порядка вызовов."""
    table = sql.Identifier(schema(), "visit")
    new_id = f"vs_{ULID()}"
    provenance = json.dumps({
        "origin": "lab_upload", "source_id": None, "extraction": None,
        "model": None, "prompt_version": None, "source_ref": source_ref,
    })
    cur.execute(
        sql.SQL(
            "INSERT INTO {table} (id, ts_event, provenance, verification, title, raw_text, extraction_status) "
            "VALUES (%s, %s, %s, 'confirmed', %s, %s, 'not_started') "
            "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
        ).format(table=table),
        (new_id, ts_event, provenance, title, raw_text),
    )
    row = cur.fetchone()
    if row is not None:
        write_journal(cur, "visit", row[0], "create",
                      diff={"title": title, "ts_event": str(ts_event), "source_ref": source_ref},
                      link_back=True)
        return row[0]
    cur.execute(
        sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s").format(table=table),
        (source_ref,),
    )
    return cur.fetchone()[0]


class VisitSyncRequest(BaseModel):
    source_ref: str  # внешний Visit_ID
    title: Optional[str] = None
    raw_text: Optional[str] = None
    ts_event: datetime


class VisitSyncResponse(BaseModel):
    id: str
    created: bool


@app.post("/visits/sync", response_model=VisitSyncResponse)
def visits_sync(req: VisitSyncRequest) -> VisitSyncResponse:
    """Гарантирует существование card.visit — вызывается даже когда в документе не
    нашлось ни одного распознанного показателя (Marker_ID='_none' в источнике), иначе
    сам факт визита теряется молча."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s")
                .format(table=sql.Identifier(schema(), "visit")),
                (req.source_ref,),
            )
            existed = cur.fetchone() is not None
            visit_id = _ensure_visit(cur, req.source_ref, req.title, req.raw_text, req.ts_event)
        conn.commit()
    return VisitSyncResponse(id=visit_id, created=not existed)


class LabResultSyncRequest(BaseModel):
    visit_source_ref: str
    visit_ts_event: datetime
    marker_key: str
    marker_label: Optional[str] = None
    value_num: Optional[float] = None
    value_text: Optional[str] = None
    unit: Optional[str] = None
    ref_min: Optional[float] = None
    ref_max: Optional[float] = None


class LabResultSyncResponse(BaseModel):
    id: str
    created: bool
    visit_id: str


@app.post("/labs/result", response_model=LabResultSyncResponse)
def labs_result_sync(req: LabResultSyncRequest) -> LabResultSyncResponse:
    """Один показатель одного визита -> lab_result + fact. Идемпотентно по
    (visit_source_ref, marker_key) — повторная загрузка того же документа не плодит
    дубли (совпадает с ON CONFLICT (Visit_ID, Marker_ID) у health.results). Гарантирует
    визит попутно (_ensure_visit) — лаборатория не ждёт отдельного вызова /visits/sync."""
    source_ref = f"{req.visit_source_ref}:{req.marker_key}"
    lab_table = sql.Identifier(schema(), "lab_result")
    fact_table = sql.Identifier(schema(), "fact")

    with get_conn() as conn:
        with conn.cursor() as cur:
            visit_id = _ensure_visit(cur, req.visit_source_ref, None, None, req.visit_ts_event)

            new_id = f"lb_{ULID()}"
            provenance = json.dumps({
                "origin": "lab_upload", "source_id": None, "extraction": None,
                "model": None, "prompt_version": None, "source_ref": source_ref,
            })
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} (id, ts_event, provenance, verification, visit_id, marker_key, marker_label, value_num, value_text, unit, ref_min, ref_max) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
                ).format(table=lab_table),
                (new_id, req.visit_ts_event, provenance, visit_id, req.marker_key, req.marker_label,
                 req.value_num, req.value_text, req.unit, req.ref_min, req.ref_max),
            )
            row = cur.fetchone()
            created = row is not None

            if created:
                result_id = row[0]
                write_journal(cur, "lab_result", result_id, "create",
                              diff={"marker_key": req.marker_key, "marker_label": req.marker_label,
                                    "value_num": req.value_num, "value_text": req.value_text,
                                    "unit": req.unit, "source_ref": source_ref},
                              link_back=True)
                index_entity(cur, "lab_marker", req.marker_key, result_id, "lab_result")

                fact_provenance = json.dumps({
                    "origin": "lab", "source_id": None, "extraction": None,
                    "model": None, "prompt_version": None, "source_ref": source_ref,
                })
                fact_id = f"f_{ULID()}"
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {table} (id, ts_event, provenance, verification, metric_key, value_num, value_text, unit) "
                        "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ).format(table=fact_table),
                    (fact_id, req.visit_ts_event, fact_provenance, "lab:" + req.marker_key,
                     req.value_num, req.value_text, req.unit),
                )
                if cur.fetchone() is not None:
                    write_journal(cur, "fact", fact_id, "create",
                                  diff={"metric_key": "lab:" + req.marker_key, "value_num": req.value_num,
                                        "value_text": req.value_text, "unit": req.unit, "source_ref": source_ref},
                                  link_back=True)
            else:
                cur.execute(
                    sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s").format(table=lab_table),
                    (source_ref,),
                )
                result_id = cur.fetchone()[0]
        conn.commit()
    return LabResultSyncResponse(id=result_id, created=created, visit_id=visit_id)


@app.post("/recommendations/sync", response_model=RecommendationSyncResponse)
def recommendations_sync(req: RecommendationSyncRequest) -> RecommendationSyncResponse:
    """Rec1 (закрытие находки №1): рекомендация — строка с id с момента создания, не
    JSON-блок в прозе. Идемпотентно по source_ref. Используется и Advisor'ом (новые
    рекомендации, дуальная запись рядом с Recommendations_Log) и миграцией
    (action_loops legacy_import)."""
    return sync_recommendation(req)


@app.post("/recommendations/propose", response_model=ProposeResponse)
def recommendations_propose(req: ProposeRequest) -> ProposeResponse:
    """Gap 2 (CARD_ARCHITECTURE_PLAN §5, П3 §2.1): единственный путь рождения НОВОЙ
    рекомендации советника — G1-G6 детерминированно ДО записи rc_. /recommendations/sync
    остаётся для миграции/структурных источников, которым ворота не нужны (их данные
    уже прошли собственную валидацию до card-service)."""
    return propose_recommendation(req)


@app.post("/recommendations/{rec_id}/evaluate", response_model=EvaluateResponse)
def recommendations_evaluate(rec_id: str) -> EvaluateResponse:
    """Запускает движок вердиктов (П3 §4) для одной рекомендации, пишет rv_.
    Предыдущий current-вердикт того же цикла помечается superseded, не удаляется —
    append-only история вердиктов."""
    return evaluate_recommendation(rec_id)


@app.get("/recommendations/loops", response_model=list[ActionLoop])
def recommendations_loops(limit: int = 3) -> list[ActionLoop]:
    """Замена прозе-парсеру в Build Health JSON — тот же shape, что дашборд ждал
    раньше, посчитан один раз при evaluate(), не при каждом открытии дашборда."""
    return get_loops(limit=limit)


class ContextRequest(BaseModel):
    mode: Literal["question", "watchdog", "health_check", "vitrine"] = "question"
    text: Optional[str] = None
    budget: int = 2000


@app.post("/context")
def context_endpoint(req: ContextRequest) -> dict:
    """П4 §5: get_context(). Единственный путь сборки контекста для советника —
    браслет + горячее ВСЕГДА (M4), холодное — только если в text нашлись сущности.
    missing[] обязателен в ответе (C1) — вызывающий обязан его учитывать, не
    додумывать за модель, что не искалось."""
    with get_conn() as conn, conn.cursor() as cur:
        result = get_context(cur, req.mode, {"text": req.text} if req.text else {}, req.budget)
        conn.commit()  # touch_access внутри get_context пишет last_accessed/access_count
    return result


@app.get("/memory/summary")
def memory_summary() -> dict:
    """П4 §10: 'что ты помнишь' — детерминированный рендер карты, НЕ пересказ LLM.
    Браслет + горячий дайджест, без holodного (holodное — по конкретному вопросу,
    не для общего 'что ты обо мне помнишь')."""
    with get_conn() as conn, conn.cursor() as cur:
        result = get_context(cur, "vitrine", {})
    return {"bracelet": result["bracelet"], "hot": result["hot"], "meta": result["meta"]}


@app.get("/objects/{object_type}/{object_id}")
def get_object_endpoint(object_type: str, object_id: str) -> dict:
    """П4 §4.4: rehydration — полная запись по требованию, отдельным вызовом."""
    with get_conn() as conn, conn.cursor() as cur:
        obj = get_object(cur, object_type, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="объект не найден")
    return obj


@app.get("/health-check/pre-archive")
def pre_archive_check() -> list[dict]:
    """П4 §6.2: кандидаты на уход из умолчаний видимости. Пока не подключено к
    расписанию (health-check/П8 не реализован как процесс) — вызывать вручную или
    подключить простым n8n Schedule, когда понадобится регулярность."""
    with get_conn() as conn, conn.cursor() as cur:
        return run_pre_archive_check(cur)


class RedFlagClassifyRequest(BaseModel):
    text: str
    prior_replies: list[str] = []


@app.post("/redflag/classify")
def redflag_classify(req: RedFlagClassifyRequest) -> dict:
    """П5 §4 — слой B изолированно (контекстно-свободно, без карты — §1.3).
    Только распознаёт, не пишет rf_ — это решает вызывающий через /redflag/evaluate."""
    return redflag_classify_b(req.text, req.prior_replies).model_dump()


class RedFlagEvaluateRequest(BaseModel):
    text: str
    source_id: Optional[str] = None
    layer_b: Optional[dict] = None  # результат /redflag/classify, если уже посчитан


@app.post("/redflag/evaluate")
def redflag_evaluate(req: RedFlagEvaluateRequest) -> dict:
    """П5 §1-§6 — полный союз A + bracelet-cross (детерминированно, здесь) + B
    (если передан). Пишет rf_event/rf_session при срабатывании (F8 — сессии, не
    дублирующиеся эскалации)."""
    layer_b_obj = LayerBResult(**req.layer_b) if req.layer_b else None
    with get_conn() as conn, conn.cursor() as cur:
        out = evaluate_and_record(cur, req.text, req.source_id, layer_b_obj)
        conn.commit()
    return out


@app.get("/redflag/layer-c")
def redflag_layer_c_check() -> list[dict]:
    """П5 §5 — слой C, фактовый (не текстовый), периодический вызов (не в
    конвейере диалога). Пишет rf_event/session при срабатывании."""
    with get_conn() as conn, conn.cursor() as cur:
        hits = run_layer_c(cur)
        recorded = []
        for h in hits:
            result = {"level": h["level"], "category": h["category"], "source": "C",
                      "rule_ref": h["rule"], "confidence": None, "context_note": h["message"]}
            rec = record_rf_event(cur, result)
            recorded.append({**h, "recorded": rec})
        conn.commit()
    return recorded


class ProcessResponse(BaseModel):
    written: list[dict]
    questions: list[str]
    flags: dict


@app.post("/process/{source_id}", response_model=ProcessResponse)
def process_endpoint(source_id: str) -> ProcessResponse:
    """process(src_id) -> {written, questions, flags} — контракт П1 §5. Отдельно
    от /ingest: сырьё уже сохранено раньше и переживёт сбой на этом шаге (extraction
    упал дважды -> ручная очередь, П2 §6, не теряем данные)."""
    try:
        result = process_source(source_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ProcessResponse(**result)


@app.get("/doctor/health")
def doctor_health() -> dict:
    return {"status": "ok", "component": "doctor", "phase": 3}


@app.post("/doctor/turn", status_code=202)
def doctor_turn(background_tasks: BackgroundTasks, update: dict = Body(...)) -> dict:
    """Сырой Telegram update (план §3.2, шаг 1 — временный HTTP-хоп из n8n вместо
    прямого приёма; шаг 2 переносит приём в card-service, этот эндпоинт не меняется).
    Отвечает 202 сразу — Телеграм/n8n не должны ждать агентный цикл (сейчас, Phase 1,
    ждать особо нечего, но контракт станет важен с Phase 4)."""
    background_tasks.add_task(handle_update, update)
    return {"accepted": True}
