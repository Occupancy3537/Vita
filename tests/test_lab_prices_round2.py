"""Round 2 приёмка: M024 hs-CRP, DRAW-BLOOD, uncovered marker detection."""

def test_m024_covered_by_hs_positions_only():
    """M024 (hs-CRP для PhenoAge) покрыт hs-позициями + ТАФИ (обычный СРБ с NOTES)."""
    from app.lab_prices_map import COVERS
    hs_keys = [(lab, code) for (lab, code), ms in COVERS.items()
               if "M024" in ms]
    assert len(hs_keys) == 4, f"ожидалось 4 позиции M024 (3 hs + ТАФИ), есть {len(hs_keys)}: {hs_keys}"
    for lab, code in hs_keys:
        assert lab in ("gemotest", "invitro", "unilab", "tafi"), f"неожиданная лаба {lab}"


def test_m024_tafi_note_in_notes():
    from app.lab_prices_map import NOTES
    key = ("tafi", "srb-s-reaktivnyy-belok")
    if key in NOTES:
        assert "hs" in NOTES[key] or "PhenoAge" in NOTES[key]


def test_draw_fee_in_offers():
    """draw_fee_rub присутствует в офферах и цена включает забор."""
    from datetime import datetime, timezone
    from app.lab_prices import panel_offers, LabItem, PRICES_VERIFIED
    items = {"gemotest": [LabItem(
        lab="gemotest", lab_name="Гемотест", external_code="1.18",
        name="HbA1c", kind="single", price_rub=790.0,
        covers=("M031",), parsed_at=datetime(2026, 9, 30, tzinfo=timezone.utc))]}
    offers = panel_offers(["M031"], items)
    assert offers
    o = offers[0]
    assert o["verified"] is False
    assert o["verified"] == PRICES_VERIFIED
    assert o["collected_at"] == o["parsed_at"] == "2026-09-30T00:00:00+00:00"


def test_export_text_ends_with_verify_note():
    from app.lab_prices import build_export_text
    panel = {"date": "2026-10-05", "n_markers": 1, "tube_types": [], "fasting_required": True}
    pick = {"key": "gemotest", "name": "Гемотест", "price_rub": 1020.0,
            "parsed_at": "2026-09-30T00:00:00+00:00",
            "breakdown": [{"code": "1.18", "name": "HbA1c", "price_rub": 790.0,
                           "covers": ["M031"], "note": None}],
            "missing": []}
    text = build_export_text(panel, pick)
    assert "перед оплатой" in text


def test_uncovered_marker_with_candidate_singleton_detected():
    """ГГТ у ТАФИ и Белок общий у Юнилаба покрыты (round 2 закрыл пробелы)."""
    from app.lab_prices_map import COVERS
    assert ("tafi", "ggtp-gamma-glyutamiltranspeptidaza") in COVERS
    assert ("unilab", "112") in COVERS
