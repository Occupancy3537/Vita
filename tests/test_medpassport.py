"""app/medpassport.py — медпаспорт на вынос (стратегический разбор
2026-09-23). Ни одного нового источника данных: профиль/аллергии/хроника —
health.user_profile (реальные прод-данные, только форма проверяется, не
цифры — тот же принцип, что test_dashboard.py); активные ограничения —
health.patient_state; лабы — card.lab_result (card_test в тестах, реальные
контролируемые значения для проверки динамики)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import get_conn, schema
from app.main import app
from app.medpassport import _recent_labs

client = TestClient(app)


def test_dashboard_medpassport_endpoint_shape():
    r = client.get("/dashboard/medpassport", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("profile", "active_conditions", "active_meds", "recent_labs",
                "labs_out_of_range", "recent_doctor_notes"):
        assert key in body
    assert isinstance(body["active_conditions"], list)
    assert isinstance(body["active_meds"], list)
    assert isinstance(body["recent_labs"], list)


def test_dashboard_medpassport_wrong_token_forbidden():
    r = client.get("/dashboard/medpassport", params={"token": "wrong"})
    assert r.status_code == 403


def test_profile_reads_real_user_profile():
    """Форма, не цифры (реальные прод-данные Влада) — тот же принцип, что
    test_dashboard_health_endpoint_shape."""
    body = client.get("/dashboard/medpassport", params={"token": "test-dashboard-token-not-prod"}).json()
    profile = body["profile"]
    assert profile.get("name")
    assert profile.get("allergies")  # известно, что поле реально заполнено


def test_active_conditions_reads_real_patient_state():
    """Известный живой инвариант проекта: активная грыжа L5/S1 в
    health.patient_state (тот же источник, что и гейт нагрузки на дашборде —
    см. test_dashboard_today.py::test_dashboard_today_gate_blocked_on_real_data)."""
    body = client.get("/dashboard/medpassport", params={"token": "test-dashboard-token-not-prod"}).json()
    assert len(body["active_conditions"]) >= 1
    assert any("L5/S1" in (c.get("condition") or "") for c in body["active_conditions"])


TEST_VISIT_ID = "test-medpassport-visit"


def _seed_lab_result(marker_key, value, days_ago, unit="ммоль/л", ref_min=None, ref_max=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.lab_result "
            "(id, ts_event, provenance, verification, visit_id, marker_key, marker_label, value_num, unit, ref_min, ref_max) "
            "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s, %s)",
            (f"lb_test_{marker_key}_{days_ago}", datetime.now(timezone.utc) - timedelta(days=days_ago),
             '{"origin":"test"}', TEST_VISIT_ID, marker_key, marker_key, value, unit, ref_min, ref_max),
        )
        conn.commit()


def _cleanup_lab_results(marker_keys):
    with get_conn() as conn, conn.cursor() as cur:
        for mk in marker_keys:
            cur.execute(f"DELETE FROM {schema()}.lab_result WHERE marker_key = %s", (mk,))
        conn.commit()


def test_recent_labs_picks_latest_and_prev_for_dynamics():
    _seed_lab_result("test_mp_glucose", 5.2, days_ago=30)
    _seed_lab_result("test_mp_glucose", 6.8, days_ago=1)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            labs = _recent_labs(cur, limit=50)
        entry = next(l for l in labs if l["marker"] == "test_mp_glucose")
        assert entry["value"] == 6.8  # самая свежая
        assert entry["prev_value"] == 5.2  # предыдущая — для стрелки динамики
    finally:
        _cleanup_lab_results(["test_mp_glucose"])


def test_recent_labs_no_prev_when_single_reading():
    _seed_lab_result("test_mp_single", 100.0, days_ago=5)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            labs = _recent_labs(cur, limit=50)
        entry = next(l for l in labs if l["marker"] == "test_mp_single")
        assert entry["prev_value"] is None
    finally:
        _cleanup_lab_results(["test_mp_single"])


def test_recent_labs_flags_out_of_range():
    _seed_lab_result("test_mp_oor", 10.0, days_ago=1, ref_min=4.0, ref_max=6.0)
    _seed_lab_result("test_mp_ok", 5.0, days_ago=1, ref_min=4.0, ref_max=6.0)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            labs = _recent_labs(cur, limit=50)
        oor = next(l for l in labs if l["marker"] == "test_mp_oor")
        ok = next(l for l in labs if l["marker"] == "test_mp_ok")
        assert oor["out_of_range"] is True
        assert ok["out_of_range"] is False
    finally:
        _cleanup_lab_results(["test_mp_oor", "test_mp_ok"])
