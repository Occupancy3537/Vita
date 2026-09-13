"""
card-service — «мозг» медицинской карты (П1–П8 из CARD_ARCHITECTURE_PLAN_2026-09-13.md).

Phase 0: скелет. Единственная реальная обязанность на этом этапе — существовать,
отвечать на /health и (следующим шагом) принимать сырьё в /ingest. Никакой бизнес-логики
здесь пока нет и не должно быть — она появится начиная с Phase 1/2 по плану.

Сервис слушает только на 127.0.0.1 (см. docker-compose) — наружу торчит только
n8n-webhook за TLS, card-service публично не виден никогда.
"""
from fastapi import FastAPI

app = FastAPI(title="card-service", version="0.0.1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
