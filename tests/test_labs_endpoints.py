"""/labs/plan и /labs/request — тикет «оптимизатор сдачи анализов»,
2026-09-26, Часть 3.2/3.4. Тот же токен-паттерн, что /outcomes/* и
/dashboard/* (см. test_dashboard.py)."""
from datetime import date

from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app

client = TestClient(app)

TOKEN = "test-dashboard-token-not-prod"


def test_labs_plan_requires_token():
    r = client.get("/labs/plan")
    assert r.status_code == 403


def test_labs_plan_wrong_token_forbidden():
    r = client.get("/labs/plan", params={"token": "wrong"})
    assert r.status_code == 403


def test_labs_plan_shape_with_token():
    r = client.get("/labs/plan", params={"token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    for key in ("generated_at", "horizon_end", "panels", "conflicts", "beyond_horizon", "n_markers_planned"):
        assert key in body
    assert isinstance(body["panels"], list)


def test_labs_plan_not_in_nginx_whitelist_by_design():
    """Часть 3.4 — эндпоинт данных для будущей Vita-витрины, НЕ добавляется в
    публичный вайтлист nginx (K1) до появления экрана. Здесь просто фиксируем,
    что эндпоинт существует и работает на 127.0.0.1 (уже проверено выше) —
    сам факт отсутствия в nginx конфигурируется вне тестов card-service."""
    r = client.get("/labs/plan", params={"token": TOKEN})
    assert r.status_code == 200


def test_labs_request_creates_open_request():
    r = client.post("/labs/request", json={
        "marker_code": "M035", "source_type": "visit", "reason": "врач сказал через 3 месяца",
        "source_ref": "labreq_test_1", "due_date": "2026-12-01",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT marker_code, status, due_date FROM {schema()}.lab_request WHERE id = %s", (body["id"],))
        row = cur.fetchone()
    assert row == ("M035", "open", date(2026, 12, 1))


def test_labs_request_idempotent_on_source_ref():
    payload = {"marker_code": "M035", "source_type": "visit", "source_ref": "labreq_test_2"}
    r1 = client.post("/labs/request", json=payload)
    r2 = client.post("/labs/request", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["created"] is False


