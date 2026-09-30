"""Схема выходного JSON lab_scrape и совместимость с app.lab_prices_ingest."""
import json
from pathlib import Path

from scripts.lab_scrape.core import make_row, now_iso

FIX = Path(__file__).parent / "fixtures" / "lab_scrape"

REQUIRED = ("lab_code", "lab_name", "city", "external_code", "name", "category",
            "price", "currency", "turnaround_time", "composition", "url",
            "scraped_at", "source_url", "raw_ref", "city_confirmed", "verified",
            "verify_note")


def _row():
    return make_row(lab_code="tafi", ext_code="tsistatin-c", name="Цистатин С",
                    price=875.0, category=None, term="до 5 дней", composition=None,
                    url="https://tafimed.ru/catalog/tsistatin-c/",
                    source_url="https://tafimed.ru/prices/", raw_ref="raw/tafi_prices.gz",
                    city_confirmed=True, collected_at=now_iso())


def test_row_has_all_format_fields():
    r = _row()
    for f in REQUIRED:
        assert f in r, f"нет поля {f}"
    assert r["city"] == "Владивосток"
    assert r["currency"] == "RUB"
    assert r["city_confirmed"] is True
    assert r["verified"] is False
    assert "проверьте" in r["verify_note"]
    assert "T" in r["scraped_at"]  # ISO


def test_row_json_roundtrip():
    r = _row()
    parsed = json.loads(json.dumps(r, ensure_ascii=False))
    assert parsed == r


def test_parse_rows_accepts_scrape_json():
    """Совместимость: lab_prices_ingest.parse_rows ест наш формат без БД."""
    from app.lab_prices_ingest import parse_rows

    rows = [_row(), make_row(
        lab_code="gemotest", ext_code="1.18", name="Гликированный гемоглобин",
        price=790.0, category="Углеводный обмен", term="1 день",
        composition=None, url="https://gemotest.ru/vladivostok/catalog/x/",
        source_url="https://gemotest.ru/vladivostok/catalog/issledovaniya-krovi/",
        raw_ref="raw/gemotest_group.gz", city_confirmed=True, collected_at=now_iso())]
    parsed = parse_rows(rows)
    assert len(parsed) == 2
    by = {(p["lab_key"], p["external_code"]): p for p in parsed}
    assert by[("tafi", "tsistatin-c")]["price_rub"] == 875.0
    assert by[("gemotest", "1.18")]["kind"] == "single"
    assert by[("tafi", "tsistatin-c")]["city"] == "Владивосток"


def test_old_suspicious_codes_are_gone_real_ones_mapped():
    """Санкция против регресса к недостоверному сбору (после пересборки
    маппинга 30.09): старых выдуманных кодов в COVERS больше нет, а реальные
    коды сайта занесены (HbA1c: у сайта «1.18», в старом файле было «3.6»)."""
    from app.lab_prices_map import COVERS

    for old_fake in (("gemotest", "3.6"), ("gemotest", "3.30"), ("gemotest", "3.31"),
                     ("tafi", "03-52"), ("invitro", "3016")):
        assert old_fake not in COVERS, f"выдуманный код остался: {old_fake}"
    assert ("gemotest", "1.18") in COVERS        # реальный код HbA1c
    assert ("gemotest", "1.205") in COVERS       # реальный код Цистатина С
    assert _row()["external_code"] != "3.6"
