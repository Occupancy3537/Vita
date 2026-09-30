"""Юнилаб (unilab.su), Владивосток.

Разведка (2026-09-30, HTTP с VPS):
- источник: серверные карточки позиций; список URL — официальные sitemap
  города: /sitemap/analyses-vladivostok.xml (780 анализов) и
  /sitemap/complex-vladivostok.xml (102 комплекса). Пагинация каталога
  (?page=) запрещена robots.txt для всех UA — не используется.
- Владивосток фиксируется ПУТЕМ /vladivostok/ и проверяется по заголовку
  страницы («…во Владивостоке | Юнилаб»).
- антибота/капчи нет; robots.txt: /services/ и /sitemap/ разрешены.
- код позиции: «Код исследования: 849 / A09.05.221» → external_code = «849»
  (короткий код лабы — до «/», как ждёт lab_prices_ingest.parse_rows).
- цена: блок «Стоимость» (без «Взятие крови +300»); срок: «Срок выполнения».
- состав комплекса: секция «Состав» карточки.
"""
from __future__ import annotations

import re

from .core import get, make_row, now_iso, parse_price, save_raw

BASE = "https://unilab.su"
SITEMAPS = [
    ("Анализы", BASE + "/sitemap/analyses-vladivostok.xml"),
    ("Комплексы", BASE + "/sitemap/complex-vladivostok.xml"),
]
_LOC_RX = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_TITLE_RX = re.compile(r"<title>(.*?)</title>", re.S)
_CODE_RX = re.compile(r"Код исследования:\s*[§\s]*?(\d+)")
_PRICE_RX = re.compile(r"Стоимость(?:\s*§\s*)+([\d\s\u00a0\u202f]+)\s*руб", re.I)
_TERM_RX = re.compile(r"Срок выполнения:\s*(?:§\s*)*([^§<]{1,60})")
_COMPOSITION_SPLIT = re.compile(r"Состав(.*?)Подготовка", re.S)
_ITEM_URL_RX = re.compile(r"/services/(analyses|complex)/vladivostok/\d+/\d+/")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " § ", s)
    s = s.replace("&nbsp;", " ").replace("&mdash;", "—").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s)


def parse_item_urls(sitemap_text: str) -> list[str]:
    """URL карточек города из sitemap. Чистая функция."""
    out = []
    for u in _LOC_RX.findall(sitemap_text):
        u = u.replace("http://", "https://")
        if _ITEM_URL_RX.search(u):
            out.append(u)
    return out


def parse_item_page(html: str, url: str, category: str) -> dict | None:
    """Разбор карточки. Чистая функция. None — карточка не распознана
    (нет кода/цены/города); причина известна вызывающему по отдельным
    проверкам. Возвращает {code, name, price, term, composition}."""
    tm = _TITLE_RX.search(html)
    title = tm.group(1).strip() if tm else ""
    if "Владивосток" not in title and "vladivostok" not in url:
        return None
    text = _strip_tags(html)
    cm = _CODE_RX.search(text)
    if not cm:
        return None
    pm = _PRICE_RX.search(text)
    price = parse_price(pm.group(1)) if pm else None
    if price is None or price <= 0:
        return None
    name = re.sub(r"\s*,\s*цена анализа во Владивостоке.*$", "", title)
    name = re.sub(r"\s*,\s*цена во Владивостоке.*$", "", name)
    name = re.sub(r"\s*\| Юнилаб\s*$", "", name).strip()
    trm = _TERM_RX.search(text)
    comp = None
    if category == "Комплексы":
        sm2 = _COMPOSITION_SPLIT.search(text)
        if sm2:
            comp = re.sub(r" § +", "; ", sm2.group(1)).strip(" ;")[:1900] or None
    return {"code": cm.group(1), "name": name, "price": price,
            "term": (trm.group(1).strip(" §") if trm else None), "composition": comp}


def collect(session, run_dir: str, stats: dict, rejects: list[dict]) -> list[dict]:
    rows: list[dict] = []
    items: list[tuple[str, str]] = []
    for label, sm_url in SITEMAPS:
        status, text = get(session, sm_url)
        stats["requests"] += 1
        if status != 200 or not text:
            rejects.append({"lab": "unilab", "reason": f"sitemap HTTP {status}", "url": sm_url})
            continue
        save_raw(run_dir, "unilab", f"sitemap_{label}", text)
        for u in parse_item_urls(text):
            items.append((label, u))
    if not items:
        rejects.append({"lab": "unilab", "reason": "sitemap не дал ни одного URL позиции"})
        return rows
    stats["city_method"] = "город в пути /vladivostok/ + «во Владивостоке» в title каждой карточки"

    seen: set[str] = set()
    for label, url in items:
        status, html = get(session, url)
        stats["requests"] += 1
        if status != 200 or not html:
            rejects.append({"lab": "unilab", "reason": f"карточка HTTP {status}", "url": url})
            continue
        # сырой ответ каждой карточки — доказательство строки (правило 2)
        iref = save_raw(run_dir, "unilab", f"item_{len(rows):05d}", html)
        parsed = parse_item_page(html, url, label)
        if parsed is None:
            rejects.append({"lab": "unilab", "reason": "карточка не распознана (нет кода/цены/города)",
                            "url": url, "raw_ref": iref})
            continue
        if parsed["code"] in seen:
            continue
        seen.add(parsed["code"])
        rows.append(make_row(
            lab_code="unilab", ext_code=parsed["code"], name=parsed["name"],
            price=parsed["price"], category=label, term=parsed["term"],
            composition=parsed["composition"], url=url, source_url=url,
            raw_ref=iref, city_confirmed=True, collected_at=now_iso()))
        stats["found"] += 1
    stats["accepted"] = len(rows)
    return rows
