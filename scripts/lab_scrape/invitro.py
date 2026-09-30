"""Инвитро (invitro.ru), Владивосток.

Разведка (2026-09-30, HTTP с VPS + браузер для обнаружения API):
- источник: открытый JSON-API сайта (найден через Network в браузере):
    /golk/addresses/api/v1/cities?q=vladivostok       — город → cityID
    /golk/tests/api/v1/tests?cityID=..&limit=100..    — каталог анализов (total 2582)
    /golk/tests/api/v1/complexes?cityID=..            — комплексы (total 328)
    /golk/tests/api/v1/complexes/{uuid}?cityID=..     — состав комплекса
- Владивосток фиксируется cityID из cities-API, чей ответ СОДЕРЖИТ
  name="Владивосток" (raw сохраняется как доказательство); все запросы
  каталога идут с этим cityID.
- антибота/капчи нет; robots.txt не запрещает /golk/ (запрещены /search/,
  /results/ и пр. — не используются).
- код позиции: product["code"] («16», «1377TER», «ОБС266»); у позиций без
  кода — bitrix_id (стабильный id записи сайта), счётчик в meta.
- url строится по паттерну сайта /analizes/for-doctors/vladivostok/{cat}/{id}/
  и выборочно проверяется живыми запросами (счётчик в meta).
- цена = product["price"] (руб). Срок = deadline (дни) → «N дн.».
"""
from __future__ import annotations

import json
import re

from .core import get, make_row, now_iso, parse_price, save_raw

BASE = "https://www.invitro.ru"
CITIES_URL = BASE + "/golk/addresses/api/v1/cities?q=vladivostok"
TESTS_URL = BASE + "/golk/tests/api/v1/tests"
COMPLEXES_URL = BASE + "/golk/tests/api/v1/complexes"
PAGE = 100


def _city_id(session, run_dir, stats, rejects):
    """cityID Владивостока с доказательством: ответ cities-API содержит
    name="Владивосток". Иначе — None и reject."""
    status, text = get(session, CITIES_URL)
    stats["requests"] += 1
    if status != 200 or not text:
        rejects.append({"lab": "invitro", "reason": f"cities API HTTP {status}", "url": CITIES_URL})
        return None
    ref = save_raw(run_dir, "invitro", "cities", text)
    data = json.loads(text)
    for c in data.get("cities", []):
        if c.get("name") == "Владивосток":
            stats["city_method"] = f"golk cities API name='Владивосток', cityID={c['id']} (raw: {ref})"
            return c["id"]
    rejects.append({"lab": "invitro", "reason": "Владивосток не найден в cities API",
                    "url": CITIES_URL, "raw_ref": ref})
    return None


def _paginated(session, url, city_id, run_dir, stats, rejects, label):
    """Полный листинг с пагинацией; возвращает (products, ref последней страницы)."""
    out, offset, last_ref = [], 0, ""
    while True:
        page_url = f"{url}?cityID={city_id}&limit={PAGE}&offset={offset}"
        status, text = get(session, page_url)
        stats["requests"] += 1
        if status != 200 or not text:
            rejects.append({"lab": "invitro", "reason": f"{label}: HTTP {status}", "url": page_url})
            break
        last_ref = save_raw(run_dir, "invitro", f"{label}_{offset:05d}", text)
        d = json.loads(text)
        batch = []
        for b in (d.get("data") or []):
            if isinstance(b, dict):
                batch.extend(b.get("products", []))
        out.extend(batch)
        total = int(d.get("total") or 0)
        offset += PAGE
        if not batch or offset >= total:
            break
    return out, last_ref


def _url_for(p) -> str | None:
    cat_bx, pid = p.get("category_bitrix_id"), p.get("bitrix_id")
    if not (cat_bx and pid):
        return None
    return f"{BASE}/analizes/for-doctors/vladivostok/{cat_bx}/{pid}/"


def _term(deadline) -> str | None:
    return f"{deadline} дн." if isinstance(deadline, int) and deadline > 0 else None


def collect(session, run_dir: str, stats: dict, rejects: list[dict]) -> list[dict]:
    rows: list[dict] = []
    city_id = _city_id(session, run_dir, stats, rejects)
    if not city_id:
        return rows

    seen: set[str] = set()
    checks_ok = checks_total = 0

    def add_row(p, category, source_url, raw_ref, composition=None):
        nonlocal checks_ok, checks_total
        code = str(p.get("code") or "").strip() or str(p.get("bitrix_id") or "")
        if not code:
            rejects.append({"lab": "invitro", "reason": "позиция без кода и bitrix_id",
                            "name": p.get("title")})
            return
        if code in seen:
            rejects.append({"lab": "invitro", "reason": f"дубль кода {code}",
                            "name": p.get("title")})
            return
        seen.add(code)
        price = parse_price(p.get("price"))
        name = (p.get("title") or "").strip()
        url = _url_for(p)
        # живая выборочная проверка построенного url (первые 10 позиций)
        if url and checks_total < 10:
            checks_total += 1
            st, _ = get(session, url)
            stats["requests"] += 1
            if st == 200:
                checks_ok += 1
        if price is None or price <= 0:
            rejects.append({"lab": "invitro", "reason": f"цена не разобрана: {p.get('price')!r}",
                            "name": name, "raw_ref": raw_ref})
            return
        rows.append(make_row(
            lab_code="invitro", ext_code=code, name=name, price=price,
            category=category, term=_term(p.get("deadline")), composition=composition,
            url=url, source_url=source_url, raw_ref=raw_ref,
            city_confirmed=True, collected_at=now_iso()))
        stats["found"] += 1

    # --- анализы ---
    products, ref = _paginated(session, TESTS_URL, city_id, run_dir, stats, rejects, "tests")
    for p in products:
        add_row(p, "Анализы", f"{TESTS_URL}?cityID={city_id}&limit={PAGE}&offset=0", ref)
    stats["accepted"] = len(rows)

    # --- комплексы (состав — из детали каждого) ---
    products, ref = _paginated(session, COMPLEXES_URL, city_id, run_dir, stats, rejects, "complexes")
    for p in products:
        comp = None
        status, text = get(session, f"{COMPLEXES_URL}/{p.get('id')}?cityID={city_id}")
        stats["requests"] += 1
        if status == 200 and text:
            cref = save_raw(run_dir, "invitro", f"complex_{re.sub(r'[^0-9a-f]', '', p.get('id', ''))[:12]}", text)
            detail = json.loads(text)
            titles = sorted({(t.get("title") or "").strip() for t in (detail.get("tests") or [])
                             if t.get("title")})
            comp = "; ".join(titles)[:1900] or None
        add_row(p, "Комплексы", f"{COMPLEXES_URL}?cityID={city_id}&limit={PAGE}&offset=0", ref, comp)
    stats["accepted"] = len(rows)
    stats["url_pattern_checks"] = f"{checks_ok}/{checks_total} HTTP 200"
    return rows
