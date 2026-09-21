"""app/phenoage_calc.py — порт n8n PhenoAge Calc (2026-09-21, найден при
проверке "можно ли убрать n8n" — Levine 2018 formula). Юниты на чистую
формулу + сборку visit_map + компоновку итогового результата, плюс
интеграционный тест записи в health.phenoage_log (реальная таблица,
тестовый date/formula_version, cleanup)."""
import pytest

from app import phenoage_calc as pa
from app.db import get_conn


# --- чистые хелперы -----------------------------------------------------------

def test_num_handles_comma_and_spaces():
    assert pa._num("47,2") == 47.2
    assert pa._num("1 234,5") == 1234.5
    assert pa._num("") is None
    assert pa._num(None) is None


def test_d10_handles_dot_and_iso_formats():
    assert pa._d10("15.06.2026") == "2026-06-15"
    assert pa._d10("2026-06-15") == "2026-06-15"
    assert pa._d10(None) == ""


def test_key_for_marker_id_by_id():
    assert pa.key_for_marker_id("M003", {}) == "gluc"
    assert pa.key_for_marker_id("M039", {}) == "wbc"


def test_key_for_marker_id_by_name_fallback():
    assert pa.key_for_marker_id("M999", {"M999": "Глюкоза (доп. метод)"}) == "gluc"


def test_key_for_marker_id_none_when_unmatched():
    assert pa.key_for_marker_id("M001", {"M001": "Билирубин общий"}) is None


def test_load_ref_config_uses_default_when_sheet_empty():
    ref, version = pa.load_ref_config([])
    assert ref == pa._REF_DEFAULT
    assert "Levine2018" in version


def test_load_ref_config_parses_real_sheet_shape():
    rows = [
        ["fkey", "col", "label", "note", "row_type", "ref_healthy"],
        ["crp_unit", "", "", "Levine2018 / CRP=mg/L / TEST", "config", ""],
        ["alb", "M008", "Альбумин", "", "marker", "47"],
        ["creat", "M004", "Креатинин", "", "marker", "75"],
        ["gluc", "M003", "Глюкоза", "", "marker", "4,6"],
        ["crp", "M024", "CRP", "", "marker", "0.5"],
        ["lymph", "M062", "Лимфоциты", "", "marker", "33"],
        ["mcv", "M043", "MCV", "", "marker", "88"],
        ["rdw", "M049", "RDW", "", "marker", "12.5"],
        ["alp", "M017", "ЩФ", "", "marker", "60"],
        ["wbc", "M039", "Лейкоциты", "", "marker", "5"],
    ]
    ref, version = pa.load_ref_config(rows)
    assert version == "Levine2018 / CRP=mg/L / TEST"
    assert ref["gluc"] == 4.6


def test_load_ref_config_falls_back_when_incomplete_markers():
    rows = [["fkey", "row_type", "ref_healthy"], ["alb", "marker", "47"]]  # только 1 из 9
    ref, _ = pa.load_ref_config(rows)
    assert ref == pa._REF_DEFAULT


# --- phenoage() формула ------------------------------------------------------

def test_phenoage_healthy_reference_values_close_to_chrono_age():
    # референсные "здоровые" значения должны давать PhenoAge примерно = возрасту
    x = {**pa._REF_DEFAULT, "age": 45}
    result = pa.phenoage(x)
    assert 35 < result < 55  # не точное равенство (формула нелинейна), но разумный диапазон


def test_phenoage_worse_markers_increase_result():
    baseline = {**pa._REF_DEFAULT, "age": 45}
    worse = dict(baseline)
    worse["crp"] = 5.0  # сильно повышенный CRP — маркер старения
    assert pa.phenoage(worse) > pa.phenoage(baseline)


# --- build_visit_map / compute_phenoage_result --------------------------------

def _marker(mid, name):
    return {"Marker_ID": mid, "Name": name}


_MARKERS = [
    _marker("M008", "Альбумин"), _marker("M004", "Креатинин"), _marker("M003", "Глюкоза"),
    _marker("M024", "CRP"), _marker("M062", "Лимфоциты %"), _marker("M043", "MCV"),
    _marker("M049", "RDW"), _marker("M017", "Щелочная фосфатаза"), _marker("M039", "Лейкоциты"),
]

_FULL_VALUES = {"M008": 47, "M004": 75, "M003": 4.6, "M024": 0.5, "M062": 33, "M043": 88, "M049": 12.5, "M017": 60, "M039": 5}


def _results_for_visit(visit_id, values):
    return [{"Visit_ID": visit_id, "Marker_ID": mid, "Value": str(v)} for mid, v in values.items()]


def test_build_visit_map_merges_results_into_visits():
    visits = [{"Visit_ID": "V1", "Date": "01.06.2026", "Age_at_Visit": "44"}]
    results = _results_for_visit("V1", _FULL_VALUES)
    vm = pa.build_visit_map(results, visits, _MARKERS)
    assert vm["V1"]["date"] == "2026-06-01"
    assert vm["V1"]["age"] == 44
    assert len(vm["V1"]["values"]) == 9


def test_compute_phenoage_result_full_panel_gives_current():
    visits = [{"Visit_ID": "V1", "Date": "2026-06-01", "Age_at_Visit": "44"}]
    results = _results_for_visit("V1", _FULL_VALUES)
    result = pa.compute_phenoage_result(results, visits, _MARKERS, pa._REF_DEFAULT, "test-formula", today="2026-06-01")
    assert "phenoage" in result["current"]
    assert result["current"]["formula_version"] == "test-formula"
    assert len(result["rows"]) >= 1


def test_compute_phenoage_result_missing_markers_reports_missing():
    visits = [{"Visit_ID": "V1", "Date": "2026-06-01", "Age_at_Visit": "44"}]
    partial = dict(_FULL_VALUES)
    del partial["M024"]  # без CRP
    results = _results_for_visit("V1", partial)
    result = pa.compute_phenoage_result(results, visits, _MARKERS, pa._REF_DEFAULT, "test-formula", today="2026-06-01")
    assert "missing" in result["current"]
    assert "crp" in result["missing_for_current"]


def test_compute_phenoage_result_no_visits_returns_empty():
    result = pa.compute_phenoage_result([], [], _MARKERS, pa._REF_DEFAULT, "test-formula")
    assert result["series"] == []
    assert result["rows"] == []


# --- write_phenoage_row (реальная таблица, тестовый ключ, cleanup) ------------

TEST_DATE = "1999-12-25"
TEST_FORMULA = "test-formula-cleanup"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.phenoage_log WHERE date = %s AND formula_version = %s", (TEST_DATE, TEST_FORMULA))
        conn.commit()


def test_write_phenoage_row_skips_when_pk_missing():
    with get_conn() as conn, conn.cursor() as cur:
        wrote = pa.write_phenoage_row(cur, {"date": None, "formula_version": TEST_FORMULA, "phenoage": 40})
        assert wrote is False


def test_write_phenoage_row_upserts_by_date_and_formula():
    row = {"date": TEST_DATE, "formula_version": TEST_FORMULA, "chrono_age": 44, "phenoage": 36, "delta": -8}
    with get_conn() as conn, conn.cursor() as cur:
        assert pa.write_phenoage_row(cur, row) is True
        conn.commit()
        cur.execute("SELECT phenoage FROM health.phenoage_log WHERE date = %s AND formula_version = %s", (TEST_DATE, TEST_FORMULA))
        assert cur.fetchone() == ("36",)

    row2 = dict(row, phenoage=38)
    with get_conn() as conn, conn.cursor() as cur:
        pa.write_phenoage_row(cur, row2)
        conn.commit()
        cur.execute("SELECT count(*), phenoage FROM health.phenoage_log WHERE date = %s AND formula_version = %s GROUP BY phenoage", (TEST_DATE, TEST_FORMULA))
        assert cur.fetchone() == (1, "38")  # перезаписал, не задвоил
