"""Дымовой тест Phase 0 — сервис жив и отвечает. Первый тест в проекте: до этого
момента у card-service (и вообще у medical-логики системы) не было ни одного
автоматического теста — это тот самый пробел, который Phase -1 закрывает."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
