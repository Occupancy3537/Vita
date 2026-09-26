"""app/lab_catalog.py — словарь анализов + текстовый скан рекомендаций
(тикет «оптимизатор сдачи анализов», 2026-09-26, Часть 1). Чистые функции,
БД не нужна."""
from app.lab_catalog import (
    LAB_CATALOG,
    PHENOAGE_PANEL_MARKERS,
    find_markers_in_text,
    parse_relative_days,
)


def test_catalog_has_83_entries_m001_through_m083():
    assert len(LAB_CATALOG) == 83
    assert set(LAB_CATALOG) == {f"M{i:03d}" for i in range(1, 84)}


def test_every_catalog_entry_has_required_fields():
    required = {"name", "category", "purpose", "default_interval_days", "one_time",
                "price_rub", "tube_type", "fasting_required", "confidence", "notes"}
    for code, entry in LAB_CATALOG.items():
        assert required <= set(entry), f"{code} не хватает полей: {required - set(entry)}"
        assert entry["confidence"] in ("known", "assumed")
        assert entry["name"] and entry["category"] and entry["purpose"]


def test_one_time_markers_have_no_standing_interval():
    """Часть 1.2 семантика: one_time=True <=> default_interval_days=None
    (разовое и стоящее расписание одновременно — противоречие)."""
    for code, entry in LAB_CATALOG.items():
        if entry["one_time"]:
            assert entry["default_interval_days"] is None, f"{code} разовый, но с интервалом"


def test_phenoage_panel_markers_all_in_catalog_with_known_confidence():
    assert len(PHENOAGE_PANEL_MARKERS) == 9
    for code in PHENOAGE_PANEL_MARKERS:
        assert code in LAB_CATALOG
        assert LAB_CATALOG[code]["confidence"] == "known"
        assert LAB_CATALOG[code]["default_interval_days"] == 180


def test_lpa_is_one_time_apob_is_standing():
    assert LAB_CATALOG["M079"]["one_time"] is True  # Lp(a) — генетический, один раз в жизни
    assert LAB_CATALOG["M078"]["one_time"] is False
    assert LAB_CATALOG["M078"]["default_interval_days"] == 365


# ─────────────────────────── parse_relative_days ───────────────────────────

def test_parse_relative_days_range_takes_lower_bound():
    assert parse_relative_days("пересдать через 4-6 недель") == 28


def test_parse_relative_days_single_number_weeks():
    assert parse_relative_days("контроль через 2 недели") == 14


def test_parse_relative_days_months():
    assert parse_relative_days("пересдать липидограмму через 90 дней") == 90
    assert parse_relative_days("через 3 месяца") == 90


def test_parse_relative_days_none_when_no_time_phrase():
    assert parse_relative_days("удерживать сатурацию выше нормы") is None


def test_parse_relative_days_survives_en_dash_and_em_dash():
    assert parse_relative_days("через 4–6 недель") == 28
    assert parse_relative_days("через 4—6 недель") == 28


# ─────────────────────────── find_markers_in_text ───────────────────────────

def test_find_markers_liver_panel_recommendation():
    text = ("Пересдать печёночный профиль с дробным билирубином, АЛТ, АСТ, ГГТ через 4–6 недель "
            "как закрытие пограничных значений")
    assert find_markers_in_text(text) == {"M001", "M015", "M016", "M083"}


def test_find_markers_lipid_panel_alias():
    text = "пересдать липидограмму через 90 дней"
    assert find_markers_in_text(text) == {"M009", "M010", "M011", "M012"}


def test_find_markers_handles_russian_declension_via_prefix():
    """"билирубином" (твор. падеж) должен найтись по префиксу "билирубин",
    тот же приём, что registrar.resolve_id уже использует для документов."""
    assert "M001" in find_markers_in_text("контроль билирубином и АЛТ")


def test_find_markers_no_false_positive_on_common_short_word():
    """Регрессия (найдено самостоятельно при живой проверке 2026-09-26):
    SYN содержит "по" -> M074 (протромбиновое отношение) — нужен registrar.py
    для сравнения ЦЕЛОГО имени маркера из документа, но в свободном тексте
    предложения слово "по" встречается постоянно и не должно матчиться."""
    assert find_markers_in_text("наблюдать по расписанию, по назначению врача") == set()
    assert "M074" not in find_markers_in_text("отчитаться по итогам недели")


def test_find_markers_empty_text_returns_empty_set():
    assert find_markers_in_text("") == set()
    assert find_markers_in_text(None) == set()
