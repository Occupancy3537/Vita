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
from fastapi import FastAPI, HTTPException
from psycopg import sql
from pydantic import BaseModel
from ulid import ULID

from app.db import get_conn, schema
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
                conn.commit()
                return InterventionSyncResponse(id=row[0], created=True)

            cur.execute(
                sql.SQL("SELECT id FROM {table} WHERE provenance->>'source_ref' = %s").format(table=table),
                (req.source_ref,),
            )
            existing = cur.fetchone()
            conn.commit()
            return InterventionSyncResponse(id=existing[0], created=False)


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
