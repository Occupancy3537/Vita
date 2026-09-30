"""Round 3: СРБ ТАФИ покрыт, ПТИ убран из unilab 215, DRAW-BLOOD, cron."""
def test_m024_tafi_covered_with_note():
    """СРБ ТАФИ покрыт M024 с NOTES hs-пометкой; остальные 3 лабы — только hs."""
    from app.lab_prices_map import COVERS, NOTES
    tafi_srb = ("tafi", "srb-s-reaktivnyy-belok")
    assert tafi_srb in COVERS and "M024" in COVERS[tafi_srb]
    assert tafi_srb in NOTES and "hs" in NOTES[tafi_srb]
    # остальные — только hs
    hs_only = [(l, c) for (l, c), ms in COVERS.items() if "M024" in ms]
    assert len(hs_only) == 4  # 3 hs + 1 tafi обычный
    for lab, code in hs_only:
        if lab == "tafi":
            continue
        assert lab in ("gemotest", "invitro", "unilab"), lab


def test_unilab_215_no_pti():
    """ПТИ не названа в названии 215 (ПВ,МНО,фибриноген,АЧТВ,РФМК) — не мапится."""
    from app.lab_prices_map import COVERS
    ms = COVERS.get(("unilab", "215"), [])
    assert "M077" not in ms
    assert set(ms) == {"M069", "M071", "M073"}


def test_draw_fee_rub_in_offers():
    """draw_fee_rub в оффере = DRAW_FEE_RUB для лабы."""
    from datetime import datetime, timezone
    from app.lab_prices import panel_offers, LabItem, DRAW_FEE_RUB
    items = {"gemotest": [LabItem(
        lab="gemotest", lab_name="Гемотест", external_code="1.18",
        name="HbA1c", kind="single", price_rub=790.0,
        covers=("M031",), parsed_at=datetime(2026, 9, 30, tzinfo=timezone.utc))]}
    offers = panel_offers(["M031"], items)
    o = offers[0]
    assert o["draw_fee_rub"] == DRAW_FEE_RUB["gemotest"] == 230.0
    # цена включает забор
    assert o["price_rub"] == 790.0 + 230.0
