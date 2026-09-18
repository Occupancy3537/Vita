"""app/dashboard.py::get_bioage_dashboard — порт n8n Build Bioage JSON (2026-09-19).
health.results/markers/visits/phenoage_log/lab_plan — общая прод-схема, только
чтение (тот же принцип, что test_dashboard.py::test_dashboard_health_endpoint_shape) —
юнит-тесты на чистую логику мокают вход, форма ответа проверяется на реальных данных."""
from fastapi.testclient import TestClient

from app.dashboard import (
    PHENO_MARKERS,
    _d10_text,
    _dec_year,
    _key_for_marker_id,
    _strip_pheno_prefix,
    get_bioage_dashboard,
)
from app.main import app

client = TestClient(app)


def test_d10_text_handles_dd_mm_yyyy():
    assert _d10_text("15.06.2019") == "2019-06-15"


def test_d10_text_handles_iso_already():
    assert _d10_text("2026-09-13") == "2026-09-13"


def test_d10_text_handles_slash_variant():
    assert _d10_text("2019/06/15") == "2019-06-15"


def test_d10_text_empty_is_empty_string():
    assert _d10_text(None) == ""
    assert _d10_text("") == ""


def test_dec_year_roughly_matches_fraction_of_year():
    # середина года — заметно больше .0, меньше .99
    x = _dec_year("2026-07-02")
    assert 2026.4 < x < 2026.6


def test_dec_year_none_for_bad_input():
    assert _dec_year(None) is None
    assert _dec_year("202") is None


def test_key_for_marker_id_matches_by_exact_id():
    assert _key_for_marker_id("M008", {}) == "alb"


def test_key_for_marker_id_matches_by_name_regex():
    mark_by_id = {"M999": {"Name": "Глюкоза венозная плазма"}}
    assert _key_for_marker_id("M999", mark_by_id) == "gluc"


def test_key_for_marker_id_no_match_returns_none():
    assert _key_for_marker_id("M999", {"M999": {"Name": "Что-то нерелевантное"}}) is None


def test_strip_pheno_prefix_removes_marker():
    assert _strip_pheno_prefix("PhenoAge / Биохимия") == "Биохимия"
    assert _strip_pheno_prefix("Гематология") == "Гематология"


def test_all_nine_pheno_markers_present():
    assert set(PHENO_MARKERS.keys()) == {
        "alb", "creat", "gluc", "crp", "lymph", "mcv", "rdw", "alp", "wbc",
    }


def test_dashboard_bioage_endpoint_shape():
    """Бьёт по реальной health.results/markers/visits/phenoage_log/lab_plan —
    проверяет форму, не конкретные значения (реальные лабы, меняются редко,
    но не гарантированно стабильны для теста)."""
    r = client.get("/dashboard/bioage", params={"token": "test-dashboard-token-not-prod"})
    assert r.status_code == 200
    body = r.json()
    for key in ("updated_at", "phenoage", "drivers", "history", "biomarkers",
                "out_of_range", "trends", "lab_plan", "data_note"):
        assert key in body
    assert isinstance(body["drivers"], list)
    assert isinstance(body["biomarkers"], list)
    assert set(body["trends"].keys()) == {"wbc", "glucose", "crp", "rdw"}
    # реальные данные проекта — минимум одна сдача анализов уже была (14 визитов, см. STATE.md)
    assert isinstance(body["phenoage"], dict)


def test_dashboard_bioage_wrong_token_forbidden():
    r = client.get("/dashboard/bioage", params={"token": "wrong"})
    assert r.status_code == 403


def test_get_bioage_dashboard_returns_dict_with_real_cursor():
    """Прямой вызов (не через HTTP) — убедиться, что функция не падает на
    реальных данных и возвращает согласованную структуру driver/phenoage."""
    from app.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        result = get_bioage_dashboard(cur)
    assert "phenoage" in result
    if result["phenoage"].get("value") is not None:
        # если PhenoAge посчитан — среди драйверов должны быть ровно два "total"
        # (Хроно и PhenoAge), водопад маркеров — между ними
        totals = [d for d in result["drivers"] if d["type"] == "total"]
        assert len(totals) == 2
        assert {t["label"] for t in totals} == {"Хроно", "PhenoAge"}
