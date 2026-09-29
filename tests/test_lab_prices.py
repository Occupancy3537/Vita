"""Цены лабораторий: чистое ядро (set-cover, офферы, парсер прайса) и
валидация маппинга — этапы 0–1 плана docs/PRICES_PLAN_QWEN.md (2026-09-29).

Тесты не требуют БД: app/lab_prices.min_cover/panel_offers и
app/lab_prices_ingest.parse_rows — чистые функции (докстринг lab_prices.py).
БД-путь (load_items/attach) сознательно не тестируется здесь — он покрыт
смоуком /labs/plan в test_labs_endpoints.py на тест-схеме."""
from datetime import datetime, timezone

from app.lab_catalog import LAB_CATALOG
from app.lab_prices import NOT_SEPARATELY_ORDERABLE, LabItem, min_cover, panel_offers
from app.lab_prices_ingest import detect_kind, parse_rows
from app.lab_prices_map import COVERS

T0 = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)


def _it(lab, ext, name, price, covers, kind="single"):
    return LabItem(lab=lab, lab_name=lab, external_code=ext, name=name, kind=kind,
                   price_rub=float(price), covers=tuple(covers), parsed_at=T0)


# ─────────────────────────── set cover ───────────────────────────

def test_complex_beats_sum_of_singles():
    # Комплекс 890 за M021+M022 дешевле двух точечных по 350+550=900
    items = [
        _it("gemotest", "9.1", "ПСА общий", 550, ["M021"]),
        _it("gemotest", "9.2", "ПСА свободный", 350, ["M022", "M023"]),
        _it("gemotest", "27.x", "ПСА-комплекс", 890, ["M021", "M022"], kind="complex"),
    ]
    sol = min_cover(["M021", "M022"], items)
    assert sol is not None
    assert [it.external_code for it in sol["items"]] == ["27.x"]
    assert sol["cost"] == 890.0
    assert sol["missing"] == []


def test_single_covers_are_preferred_when_cheaper():
    # Точечные 190+250 дешевле комплекса 1190
    items = [
        _it("l", "a", "МНО", 190, ["M073"]),
        _it("l", "b", "ПТИ", 250, ["M077"]),
        _it("l", "c", "Коагулограмма", 1190, ["M069", "M071", "M073", "M077"], kind="complex"),
    ]
    sol = min_cover(["M073", "M077"], items)
    assert [it.external_code for it in sol["items"]] == ["a", "b"]
    assert sol["cost"] == 440.0


def test_honest_missing_when_lab_does_not_do_marker():
    # ЛП(а) у лабы нет ни одной позицией — уходит в missing, а не додумывается
    items = [_it("l", "a", "Креатинин", 280, ["M004"])]
    sol = min_cover(["M004", "M079"], items)
    assert sol is not None
    assert sol["missing"] == ["M079"]
    assert sol["cost"] == 280.0


def test_oak_item_covers_many_codes():
    # ОАК одной позицией закрывает всю группу — вместо 28 «анализов»
    oak = ["M039", "M040", "M041", "M042", "M043", "M044", "M045", "M046", "M048", "M049"]
    items = [_it("l", "1.1", "ОАК", 610, oak)]
    items += [_it("l", f"s{i}", f"маркер {c}", 200, [c]) for i, c in enumerate(oak)]
    sol = min_cover(oak[:6], items)
    assert [it.external_code for it in sol["items"]] == ["1.1"]
    assert sol["cost"] == 610.0


def test_panel_offers_sorting_and_cheapest_flag():
    codes = ["M004", "M027"]
    items_by_lab = {
        "gemotest": [
            _it("gemotest", "3.11", "Креатинин", 280, ["M004"]),
            _it("gemotest", "3.31", "Ферритин", 690, ["M027"]),
        ],
        "unilab": [
            _it("unilab", "117", "Креатинин", 310, ["M004"]),
            _it("unilab", "142", "Ферритин", 790, ["M027"]),
        ],
        "invitro": [
            _it("invitro", "2209", "Креатинин", 280, ["M004"]),
            # ферритина у Инвитро «нет» → лаба с дыркой уходит в конец списка
            _it("invitro", "2245", "Ферритин", 860, ["M027"]),
        ],
    }
    del items_by_lab["invitro"][1]
    offers = panel_offers(codes, items_by_lab)
    assert [o["key"] for o in offers] == ["gemotest", "unilab", "invitro"]
    assert offers[0]["cheapest"] is True
    assert offers[0]["price_rub"] == 970.0
    assert offers[2]["missing"] == ["Ферритин"]
    assert not offers[2]["cheapest"]


def test_panel_offers_deterministic():
    codes = ["M004", "M010", "M027"]
    items_by_lab = {
        "gemotest": [
            _it("gemotest", "3.11", "Креатинин", 280, ["M004"]),
            _it("gemotest", "3.1", "Холестерин общий", 190, ["M010"]),
            _it("gemotest", "3.31", "Ферритин", 690, ["M027"]),
        ],
        "unilab": [
            _it("unilab", "117", "Креатинин", 310, ["M004"]),
            _it("unilab", "125", "Холестерин общий", 310, ["M010"]),
            _it("unilab", "142", "Ферритин", 790, ["M027"]),
        ],
    }
    a = panel_offers(codes, items_by_lab)
    b = panel_offers(list(reversed(codes)), items_by_lab)  # порядок входа не влияет
    assert a == b


def test_panel_offers_empty_codes():
    assert panel_offers([], {"gemotest": []}) == []


def test_not_orderable_markers_do_not_make_holes():
    """M047/M060 (цветовой показатель, палочкоядерные) не продаёт ни одна лаба —
    они не должны превращать ВСЕ лабы в «с дыркой» и ломать правило «полные
    вперёд». Панель: ОАК-группа + оба легаси-кода; оффер = 12 из 12, missing пуст."""
    oak = ["M039", "M043", "M049", "M062"]
    panel = oak + sorted(NOT_SEPARATELY_ORDERABLE)  # 6 маркеров, из них 2 не заказные
    items_by_lab = {
        "gemotest": [_it("gemotest", "1.1", "ОАК", 610, oak)],
        "invitro": [_it("invitro", "2852", "ОАК без диффа", 255, ["M039", "M043", "M049"])],
    }
    offers = panel_offers(panel, items_by_lab)
    assert [o["key"] for o in offers] == ["gemotest", "invitro"]  # полная лаба первая
    assert offers[0]["covered"] == 6 and offers[0]["n"] == 6
    assert offers[0]["missing"] == []
    # у Инвитро реальная дыра (нет M062), легаси-коды в missing не попадают
    assert offers[1]["missing"] == ["Лимфоциты %"]


def test_not_orderable_only_panel_has_no_price():
    assert panel_offers(sorted(NOT_SEPARATELY_ORDERABLE), {"l": [_it("l", "a", "x", 100, ["M039"])]}) == []


# ─────────────────────────── парсер прайса ───────────────────────────

def test_parse_rows_kind_detection():
    rows = parse_rows([
        {"lab_code": "gemotest", "lab_name": "Гемотест", "city": "Владивосток",
         "external_code": "3.5", "name": "Глюкоза", "category": "Углеводный обмен",
         "price": 240, "currency": "RUB", "turnaround_time": "1 день",
         "composition": "-", "url": "https://x", "scraped_at": "2026-09-29T05:04:59.330198+00:00"},
        {"lab_code": "gemotest", "lab_name": "Гемотест", "city": "Владивосток",
         "external_code": "27.5", "name": "Биохимия 13", "category": "Комплексные исследования",
         "price": 1690, "currency": "RUB", "turnaround_time": "1 день",
         "composition": "АЛТ, АСТ, Билирубин общий", "url": "https://x",
         "scraped_at": "2026-09-29T05:04:59.330198+00:00"},
    ])
    by = {r["external_code"]: r for r in rows}
    assert by["3.5"]["kind"] == "single"
    assert by["27.5"]["kind"] == "complex"
    assert by["3.5"]["price_rub"] == 240.0
    assert by["3.5"]["parsed_at"].year == 2026


def test_parse_rows_rejects_broken_record():
    try:
        parse_rows([{"lab_code": "gemotest", "external_code": "3.5", "name": "Глюкоза"}])
    except ValueError:
        pass
    else:
        raise AssertionError("битая запись должна ронять импорт, а не пропускаться молча")


def test_detect_kind_by_name_prefix():
    assert detect_kind("Комплексный анализ на 8 витаминов", "Витамины", "-") == "complex"
    assert detect_kind("Глюкоза", "Углеводный обмен", "") == "single"


# ─────────────────────────── валидация маппинга ───────────────────────────

def test_map_codes_exist_in_catalog():
    """Опечатка в коде маркера = тихая дыра в цене; ловим её здесь, а не на
    живом экране. Заодно проверяем, что ни одна позиция не замаплена пустой."""
    for key, codes in COVERS.items():
        assert codes, f"{key}: пустое покрытие"
        for c in codes:
            assert c in LAB_CATALOG, f"{key}: код {c} отсутствует в LAB_CATALOG"


def test_map_codes_are_unique_per_position():
    for key, codes in COVERS.items():
        assert len(codes) == len(set(codes)), f"{key}: дубли кодов внутри позиции"
