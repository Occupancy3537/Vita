"""Гемотест (gemotest.ru), Владивосток.

Разведка (2026-09-30, HTTP с VPS):
- источник: серверный HTML каталога; карточки позиций несут код/название/цену
  в data-атрибутах (data-eec-id / data-eec-name / data-eec-price) — надёжнее
  разбора вёрстки. Карточки собираются со страниц 11 групп
  /vladivostok/catalog/{group}/ (по одной странице на группу, без пагинации).
- Владивосток фиксируется ПУТЕМ /vladivostok/... (город в URL) и проверяется
  наличию «Владивосток» в каждом ответе.
- антибота/капчи нет; robots.txt: секции «User-agent: *» нет вовсе — для
  нашего UA ограничений нет (Yandex-секция на нас не распространяется).
- код позиции: «Код на бланке» вида «1.18» (в data-eec-id бывает «1.18.» —
  хвостовая точка срезается). Срок и состав — только на карточке позиции:
  собираются обогащением для позиций с комплексным наименованием; у синглов
  turnaround_time=null (в списке группы срока нет — честный null).
"""
from __future__ import annotations

import re

from . import COMPLEX_NAME_RX
from .core import get, make_row, now_iso, parse_price, save_raw

BASE = "https://gemotest.ru"
INDEX_URL = BASE + "/vladivostok/catalog/"

_CARD_TAG_RX = re.compile(r'<div[^>]*\bid="item_\d+"[^>]*>', re.S)
_EEC_NAME = re.compile(r'data-eec-name="([^"]*)"')
_EEC_ID = re.compile(r'data-eec-id="([^"]*)"')
_EEC_PRICE = re.compile(r'data-eec-price="([^"]*)"')
_EEC_SEC = re.compile(r'data-eec-sec="([^"]*)"')
_LINK_RX = re.compile(r'href="(/vladivostok/catalog/[^"]+)"')
_GROUP_RX = re.compile(r'href="(/vladivostok/catalog/[a-z0-9-]+/)"')
_TERM_RX = re.compile(r"(\d+\s*(?:–\s*\d+\s*)?(?:раб\.\s*)?д(?:ень|н[яей])\b)", re.I)
_COMPOSITION_RX = re.compile(
    r"(?:В состав (?:входит|исследования входит)|Состав исследования)(.{80,3000}?)"
    r"(?:Как подготовиться|Подготовка|Показания|Описание|<h\d)", re.S | re.I)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&mdash;", "—").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def parse_groups(index_html: str) -> list[str]:
    """Ссылки на страницы групп из индекса каталога."""
    return sorted(set(_GROUP_RX.findall(index_html)))


def parse_group_cards(html: str) -> list[dict]:
    """Карточки группы: {code, name, price, category, path}. Чистая функция."""
    out = []
    for tag_m in _CARD_TAG_RX.finditer(html):
        tag = tag_m.group(0)
        nm, cd, pr, sec = (_EEC_NAME.search(tag), _EEC_ID.search(tag),
                           _EEC_PRICE.search(tag), _EEC_SEC.search(tag))
        if not (nm and cd and pr):
            continue
        tail = html[tag_m.end():tag_m.end() + 1200]
        lm = _LINK_RX.search(tail)
        if not lm:
            continue
        out.append({
            "code": cd.group(1).strip().rstrip("."),
            "name": nm.group(1).strip(),
            "price_raw": pr.group(1),
            "price": parse_price(pr.group(1)),
            "category": sec.group(1) if sec else None,
            "path": lm.group(1),
        })
    return out


def parse_item_enrichment(html: str) -> tuple[str | None, str | None]:
    """(срок, состав) с карточки позиции. Чистая функция; нет — None."""
    term = None
    tm = _TERM_RX.search(_strip_tags(html[:400000]))
    if tm:
        term = tm.group(1)
    comp = None
    cm = _COMPOSITION_RX.search(html)
    if cm:
        comp = _strip_tags(cm.group(1))[:1500] or None
    return term, comp


def collect(session, run_dir: str, stats: dict, rejects: list[dict]) -> list[dict]:
    rows: list[dict] = []
    status, html = get(session, INDEX_URL)
    stats["requests"] += 1
    if status != 200 or not html:
        rejects.append({"lab": "gemotest", "reason": f"каталог недоступен: HTTP {status}",
                        "url": INDEX_URL})
        return rows
    save_raw(run_dir, "gemotest", "index", html)
    groups = parse_groups(html)[:60]
    stats["groups"] = len(groups)

    seen_codes: set[str] = set()
    enrich: list[tuple[str, str]] = []  # (url, code)

    for gi, g in enumerate(groups):
        gurl = BASE + g
        status, ghtml = get(session, gurl)
        stats["requests"] += 1
        if status != 200 or not ghtml:
            rejects.append({"lab": "gemotest", "reason": f"HTTP {status}", "url": gurl})
            continue
        gref = save_raw(run_dir, "gemotest", f"group{gi:02d}", ghtml)
        if "Владивосток" not in ghtml:
            rejects.append({"lab": "gemotest", "reason": "город не подтверждён на странице группы",
                            "url": gurl, "raw_ref": gref})
            continue
        for card in parse_group_cards(ghtml):
            code = card["code"]
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            if card["price"] is None or card["price"] <= 0:
                rejects.append({"lab": "gemotest", "reason": f"цена не разобрана: {card['price_raw']!r}",
                                "url": gurl, "name": card["name"], "raw_ref": gref})
                continue
            rows.append(make_row(
                lab_code="gemotest", ext_code=code, name=card["name"],
                price=card["price"], category=card["category"],
                term=None, composition=None, url=BASE + card["path"],
                source_url=gurl, raw_ref=gref, city_confirmed=True,
                collected_at=now_iso()))
            stats["found"] += 1
            if re.search(COMPLEX_NAME_RX, card["name"], re.I):
                enrich.append((BASE + card["path"], code))
        stats["accepted"] = len(rows)

    # обогащение комплексов: срок и состав — с карточки позиции
    for url, code in enrich:
        status, ihtml = get(session, url)
        stats["requests"] += 1
        stats["enriched"] = stats.get("enriched", 0) + 1
        if status != 200 or not ihtml:
            rejects.append({"lab": "gemotest", "reason": f"обогащение HTTP {status}",
                            "url": url})
            continue
        iref = save_raw(run_dir, "gemotest", f"item_{re.sub(r'[^0-9]', '_', code)}", ihtml)
        term, comp = parse_item_enrichment(ihtml)
        # source_url/raw_ref НЕ перетираем: они указывают на источник ЦЕНЫ
        # (страницу группы); сырой ответ карточки — в raw/
        for r in rows:
            if r["external_code"] == code:
                r["turnaround_time"] = term
                r["composition"] = comp
                r["enrich_raw_ref"] = iref
                break
    return rows
