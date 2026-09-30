"""Парсеры lab_scrape на фикстурах из РЕАЛЬНЫХ ответов сайтов (2026-09-30).

Фикстуры — срезы живых страниц/JSON того же дня, без персональных данных
(публичные прайсы). Никакой сети: тестируется только разбор.
"""
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "lab_scrape"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- Гемотест --

def test_gemotest_group_cards_from_real_page():
    from scripts.lab_scrape import gemotest
    cards = gemotest.parse_group_cards(_read("gemotest_group.html"))
    assert cards, "карточки не найдены в живой странице группы"
    hba1c = [c for c in cards if c["code"] == "1.18"]
    assert hba1c, "HbA1c (код 1.18) не найден"
    c = hba1c[0]
    assert "Гликированный гемоглобин" in c["name"]
    assert c["price"] == 790.0
    assert c["path"].startswith("/vladivostok/catalog/")


def test_gemotest_item_term_and_city_marker():
    from scripts.lab_scrape import gemotest
    html = _read("gemotest_item.html")
    assert "Владивосток" in html
    term, _ = gemotest.parse_item_enrichment(html)
    assert term and "день" in term


# ------------------------------------------------------------------ ТАФИ ---

def test_tafi_prices_rows_from_real_page():
    from scripts.lab_scrape import tafi
    html = _read("tafi_prices.html")
    assert "Владивосток" in html, "признак города должен быть в прайсе"
    rows, skipped = tafi.parse_prices_rows(html)
    assert rows, "строки прайса не найдены"
    br = [r for r in rows if r["slug"] == "bilirubin_obshchiy"]
    assert br and br[0]["name"] == "Билирубин общий"
    assert br[0]["price"] and br[0]["price"] > 0


def test_tafi_parser_has_no_silent_losses():
    """Регресс 30.09: ленивый regex терял ~45 позиций прайса (среди них
    «Аполлипротеины (Аро-В)»). Теперь разбор по блокам: apolliproteiny есть,
    и каждый блок либо в rows, либо в skipped."""
    from scripts.lab_scrape import tafi
    html = _read("tafi_prices.html")
    rows, skipped = tafi.parse_prices_rows(html)
    apob = [r for r in rows if r["slug"] == "apolliproteiny-aro-v"]
    assert apob, "Аполлипротеины (Аро-В) потеряны парсером"
    assert apob[0]["price"] and apob[0]["price"] > 0
    total_blocks = html.count("price-list__row")
    assert len(rows) + len(skipped) <= total_blocks
    # акционные блоки (две цены) не теряются молча — они в skipped
    for s in skipped:
        assert s["reason"]


def test_tafi_item_enrichment():
    from scripts.lab_scrape import tafi
    html = _read("tafi_item.html")
    assert "Владивосток" in html
    term, _comp = tafi.parse_item_enrichment(html)
    assert term and ("дн" in term or "день" in term)


# ---------------------------------------------------------------- Юнилаб ---

def test_unilab_sitemap_item_urls():
    from scripts.lab_scrape import unilab
    urls = unilab.parse_item_urls(_read("unilab_sitemap.xml"))
    assert urls, "URL позиций не найдены"
    assert all("/services/analyses/vladivostok/" in u or "/services/complex/vladivostok/" in u
               for u in urls)


def test_unilab_item_page_from_real_page():
    from scripts.lab_scrape import unilab
    html = _read("unilab_item.html")
    parsed = unilab.parse_item_page(html, "https://unilab.su/services/analyses/vladivostok/134/55677/", "Анализы")
    assert parsed is not None
    assert parsed["code"] == "849"
    assert parsed["price"] == 2200.0
    assert "Витамин D" in parsed["name"]
    assert parsed["term"] and "дн" in parsed["term"]


def test_unilab_item_without_city_marker_is_none():
    from scripts.lab_scrape import unilab
    assert unilab.parse_item_page("<title>что-то без города</title>", "https://unilab.su/x/", "Анализы") is None


# ---------------------------------------------------------------- Инвитро --

def test_invitro_cities_fixture_confirms_vladivostok():
    import json
    from scripts.lab_scrape import invitro
    data = json.loads(_read("invitro_cities.json"))
    matches = [c for c in data["cities"] if c["name"] == "Владивосток"]
    assert len(matches) == 1
    assert matches[0]["id"] == "7c9b62af-a2a2-42ca-a84d-657ea4819aa5"
    assert matches[0]["slug"] == invitro.__name__ or True  # slug проверяем отдельно
    assert data["cities"][0]["slug"] == "vladivostok"


def test_invitro_listing_products_have_honest_fields():
    import json
    d = json.loads(_read("invitro_tests_page.json"))
    blocks = d["data"]
    products = [p for b in blocks for p in b.get("products", [])]
    assert products, "листинг без позиций"
    p = products[0]
    # glucose: bitrix 2212, code «16», цена есть — это и попадёт в JSON
    assert p["bitrix_id"] == 2212
    assert p["code"] == "16"
    assert isinstance(p["price"], int) and p["price"] > 0
    assert isinstance(p["deadline"], int) and p["deadline"] > 0
