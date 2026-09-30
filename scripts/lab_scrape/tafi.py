"""ТАФИ-Диагностика (tafimed.ru), Владивосток.

Разведка (2026-09-30, HTTP с VPS):
- источник: страница https://tafimed.ru/prices/ — серверный полный прайс
  (1688 строк .price-list__row: ссылка на карточку, название, цена «645 ₽»).
  Срок и состав — только на карточках позиций.
- Владивосток: сайт владивостокский, признак города проверяется в каждом
  ответе («Ваш город Владивосток» / «Владивосток»).
- антибота/капчи нет; robots.txt разрешает /prices/ и /catalog/ (запрещены
  /auth/, /basket/, /search/, /ajax/ — не используются).
- артикул (01-XX) в статической HTML карточки анализа НЕ отдаётся (найден
  только шаблон строки «Артикул:» в JS); по правилу задачи «нет кода —
  стабильный идентификатор из URL»: external_code = слаг карточки
  (например «tsistatin-c»). Помечено в meta.notes.
"""
from __future__ import annotations

import re

from . import COMPLEX_NAME_RX
from .core import get, make_row, now_iso, parse_price, save_raw

BASE = "https://tafimed.ru"
PRICES_URL = BASE + "/prices/"

_ROW_RX = re.compile(
    r'price-list__row[^>]*>.*?href="(/catalog/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>.*?'
    r'price-list__price">\s*([\d\s\u00a0\u202f]+)\s*(?:₽|руб)', re.S)
_CITY_OK = re.compile(r"Владивосток")
_TERM_RX = re.compile(r"(до\s*\d+\s*(?:раб\.\s*)?дн|\d+\s*(?:раб\.\s*)?дн\w*|1\s*день)", re.I)
_COMPOSITION_RX = re.compile(
    r"(?:В состав (?:комплекса |исследования )?входит|Состав(?:\s*комплекса)?)(.{80,3000}?)"
    r"(?:Подготовка|Как подготовиться|Показания|Правила|<h\d)", re.S | re.I)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&mdash;", "—").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def parse_prices_rows(html: str) -> list[dict]:
    """Строки полного прайса: {slug, name, price, path}. Чистая функция."""
    out, seen = [], set()
    for m in _ROW_RX.finditer(html):
        href, name, price_raw = m.group(1), m.group(2).strip(), m.group(3)
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name, "price": parse_price(price_raw),
                    "path": href})
    return out


def parse_item_enrichment(html: str) -> tuple[str | None, str | None]:
    """(срок, состав) с карточки позиции. Чистая функция; нет — None."""
    term = None
    tm = _TERM_RX.search(_strip_tags(html))
    if tm:
        term = tm.group(0)
    comp = None
    cm = _COMPOSITION_RX.search(html)
    if cm:
        comp = _strip_tags(cm.group(1))[:1500] or None
    return term, comp


def collect(session, run_dir: str, stats: dict, rejects: list[dict]) -> list[dict]:
    rows: list[dict] = []
    status, html = get(session, PRICES_URL)
    stats["requests"] += 1
    if status != 200 or not html:
        rejects.append({"lab": "tafi", "reason": f"прайс недоступен: HTTP {status}", "url": PRICES_URL})
        return rows
    ref = save_raw(run_dir, "tafi", "prices", html)
    if not _CITY_OK.search(html):
        rejects.append({"lab": "tafi", "reason": "нет признака Владивостока на /prices/",
                        "url": PRICES_URL, "raw_ref": ref})
        return rows
    stats["city_method"] = f"признак города в ответе /prices/ («Ваш город Владивосток», raw: {ref})"

    enrich: list[tuple[str, str]] = []
    for card in parse_prices_rows(html):
        if card["price"] is None or card["price"] <= 0:
            rejects.append({"lab": "tafi", "reason": f"цена не разобрана",
                            "name": card["name"], "raw_ref": ref})
            continue
        rows.append(make_row(
            lab_code="tafi", ext_code=card["slug"], name=card["name"],
            price=card["price"], category=None, term=None, composition=None,
            url=BASE + card["path"], source_url=PRICES_URL, raw_ref=ref,
            city_confirmed=True, collected_at=now_iso()))
        stats["found"] += 1
        if re.search(COMPLEX_NAME_RX, card["name"], re.I):
            enrich.append((BASE + card["path"], card["slug"]))
    stats["accepted"] = len(rows)

    # обогащение комплексов: срок и состав — с карточки позиции
    for url, slug in enrich:
        status, ihtml = get(session, url)
        stats["requests"] += 1
        stats["enriched"] = stats.get("enriched", 0) + 1
        if status != 200 or not ihtml:
            rejects.append({"lab": "tafi", "reason": f"обогащение HTTP {status}",
                            "url": url})
            continue
        iref = save_raw(run_dir, "tafi", f"item_{re.sub(r'[^0-9a-z]', '_', slug)[:40]}", ihtml)
        if not _CITY_OK.search(ihtml):
            rejects.append({"lab": "tafi", "reason": "нет признака Владивостока на карточке",
                            "url": url, "raw_ref": iref})
            continue
        term, comp = parse_item_enrichment(ihtml)
        # source_url/raw_ref НЕ перетираем: они указывают на источник ЦЕНЫ
        # (/prices/); сырой ответ карточки сохранён рядом в raw/
        for r in rows:
            if r["external_code"] == slug:
                r["turnaround_time"] = term
                r["composition"] = comp
                r["enrich_raw_ref"] = iref
                break
    return rows
