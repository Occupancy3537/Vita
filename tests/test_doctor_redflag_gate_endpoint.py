"""POST /doctor/redflag-gate — гейт-only эндпоинт (план §3.9), задействован
раньше срока 2026-09-15 как временная замена красных флагов старого доктора
на время его отключения (OpenRouter workspace daily budget)."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_emergency_text_returns_l3_and_reply():
    resp = client.post("/doctor/redflag-gate", json={"text": "новокаин, тяжело дышать"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "L3"
    assert data["emergency"] is True
    assert "скорую" in data["reply"].lower()


def test_safe_text_returns_no_emergency():
    resp = client.post("/doctor/redflag-gate", json={"text": "лёгкое покалывание после того как отлежал руку"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["emergency"] is False
    assert data["reply"] is None
