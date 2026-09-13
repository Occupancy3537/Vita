"""
card-service — «мозг» медицинской карты (П1–П8 из CARD_ARCHITECTURE_PLAN_2026-09-13.md).

Phase 0: /health + /ingest. /ingest делает РОВНО одну вещь — сохраняет сырьё в
source_message, до всякой обработки (П1 §1.1 "Сначала сырьё"). Никакого извлечения,
никакой валидации содержимого, никакого LLM здесь пока нет — это Phase 2 (write-path).

Сервис слушает только на 127.0.0.1 (см. README) — наружу торчит только n8n-webhook
за TLS, card-service публично не виден никогда.
"""
import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg import sql
from pydantic import BaseModel
from ulid import ULID

from app.db import get_conn, schema

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
