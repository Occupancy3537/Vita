"""Round 2 приёмка: M024 hs-CRP, DRAW-BLOOD, uncovered marker detection."""

def test_m024_covered_by_hs_positions_only():
    """M024 (hs-CRP для PhenoAge) покрыт ТОЛЬКО hs-позициями."""
    from app.lab_prices_map import COVERS
    hs_keys = [(lab, code) for (lab, code), ms in COVERS.items()
               if "M024" in ms]
    # Должно быть ровно 3 hs-позиции (без ТАФИ — у неё hs нет)
    assert len(hs_keys) == 3, f"ожидалось 3 hs-позиции M024, есть {len(hs_keys)}: {hs_keys}"
    for lab, code in hs_keys:
        assert lab in ("gemotest", "invitro", "unilab"), f"неожиданная лаба {lab}"
    # ТАФИ обычный СРБ — НЕ покрывает M024
    assert ("tafi", "srb-s-reaktivnyy-belok") not in hs_keys


def test_m024_tafi_note_in_notes():
    from app.lab_prices_map import NOTES
    key = ("tafi", "srb-s-reaktivnyy-belok")
    if key in NOTES:
        assert "hs" in NOTES[key] or "PhenoAge" in NOTES[key]


def test_draw_fee_in_offers():
    """draw_fee_rub присутствует в офферах и цена включает забор."""
    from datetime import datetime, timezone
    from app.lab_prices import panel_offers, LabItem
    items = {"gemotest": [LabItem(
        lab="gemotest", lab_name="Гемотест", external_code="1.18",
        name="HbA1c", kind="single", price_rub=790.0,
        covers=("M031",), parsed_at=datetime(2026, 9, 30, tzinfo=timezone.utc))]}
    offers = panel_offers(["M031"], items)
    assert offers
    o = offers[0]
    assert o["draw_fee_rub"] == 230.0
    assert o["price_rub"] == 790.0 + 230.0  # анализ + забор


def test_export_text_has_draw_fee_line():
    from app.lab_prices import build_export_text
    panel = {"date": "2026-10-05", "n_markers": 1, "tube_types": [], "fasting_required": True}
    pick = {"key": "gemotest", "name": "Гемотест", "price_rub": 1020.0,
            "parsed_at": "2026-09-30T00:00:00+00:00",
            "breakdown": [{"code": "1.18", "name": "HbA1c", "price_rub": 790.0,
                           "covers": ["M031"], "note": None}],
            "missing": []}
    text = build_export_text(panel, pick)
    assert "Взятие венозной крови — 230 ₽" in text
    assert "перед оплатой" in text


def test_uncovered_marker_with_candidate_singleton_detected():
    """Тест-предупреждение: маркер каталога не покрыт у лабы, но в прайсе
    есть одиночный анализ с подходящим названием — ловится автоматически."""
    # ГГТ у ТАФИ: код ggtp-… должен быть в COVERS
    from app.lab_prices_map import COVERS
    assert ("tafi", "ggtp-gamma-glyutamiltranspeptidaza") in COVERS
    assert "M083" in COVERS[("tafi", "ggtp-gamma-glyutamiltranspeptidaza")]
    # Белок общий у Юнилаба
    assert ("unilab", "112") in COVERS
    assert "M007" in COVERS[("unilab", "112")]
