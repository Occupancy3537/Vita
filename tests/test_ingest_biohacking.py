"""POST /ingest/biohacking — эндпоинт-обвязка над app.biohacking_ingest
(логика уже покрыта test_biohacking_ingest.py; здесь — что FastAPI-роут
действительно вызывает process_ingest и возвращает ожидаемый ответ)."""
from fastapi.testclient import TestClient

from app import biohacking_ingest as bi
from app.db import get_conn
from app.main import app

client = TestClient(app)

TEST_DATE = "1999-12-30"


def _stub_all(monkeypatch):
    monkeypatch.setattr(bi, "fetch_nutrition", lambda cur: [])
    monkeypatch.setattr(bi, "fetch_climate", lambda: [])
    monkeypatch.setattr(bi, "fetch_calendar_events", lambda d: [])
    monkeypatch.setattr(bi, "fetch_rescuetime", lambda d: [])
    monkeypatch.setattr(bi, "fetch_weather", lambda: {})
    monkeypatch.setattr(bi, "compute_pressure_deltas", lambda w: {})
    monkeypatch.setattr(bi, "sync_device_facts", lambda row: None)
    import app.anomaly_detector as ad
    monkeypatch.setattr(ad, "run_daily_check", lambda: None)


def teardown_function():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.daily_trends WHERE "Дата" = %s', (TEST_DATE,))
        cur.execute("DELETE FROM health.garmin_ingest_log WHERE date = %s", (TEST_DATE,))
        conn.commit()


def test_ingest_biohacking_writes_row_and_returns_date(monkeypatch):
    _stub_all(monkeypatch)
    r = client.post("/ingest/biohacking", json={"date": TEST_DATE, "steps": 7000})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "date": TEST_DATE}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Шаги_за_вчера" FROM health.daily_trends WHERE "Дата" = %s', (TEST_DATE,))
        assert cur.fetchone() == ("7000",)


def test_ingest_biohacking_rejects_missing_date(monkeypatch):
    _stub_all(monkeypatch)
    r = client.post("/ingest/biohacking", json={"steps": 1000})
    assert r.status_code == 422


def test_ingest_biohacking_ignores_unknown_extra_fields(monkeypatch):
    _stub_all(monkeypatch)
    r = client.post("/ingest/biohacking", json={"date": TEST_DATE, "steps": 100, "какое-то_новое_поле_гармина": 42})
    assert r.status_code == 200
