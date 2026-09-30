"""Импорт и обновление прайсов лабораторий — этап 0 плана
docs/PRICES_PLAN_QWEN.md (2026-09-29).

Источник сегодня — сводный JSON скрейпа (агент Влада, Google Drive → seed):
    [{"lab_code", "lab_name", "city", "external_code", "name", "category",
      "price", "currency", "turnaround_time", "composition", "url",
      "scraped_at"}, ...]
Схема записи у всех лаб одна, поэтому парсеру всё равно — один файл или
по-лабный. Сайт-скрейперы (недельный scheduler) будут дописаны адаптерами
в этот же модуль и вызовут тот же upsert() — формат входа не меняется.

Молчаливого матчинга нет: позиции прайса попадают в card.lab_item «как есть»,
покрытие кодами каталога — только через ручной app/lab_prices_map.COVERS.
Очередь ручной приёмки — не таблица, а отчёт:
    python -m app.lab_prices_ingest report
печатает незмапленные single-позиции с fuzzy-подсказками против названий
LAB_CATALOG — смотришь глазами, добавляешь строку в lab_prices_map.py.

CLI:
  python -m app.lab_prices_ingest seed <file.json> [file2.json ...]
  python -m app.lab_prices_ingest report
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from datetime import datetime

from app.lab_catalog import LAB_CATALOG
from app.lab_prices_map import COVERS

_COMPLEX_CATEGORY_RX = re.compile(r"комплекс|чек", re.I)


def detect_kind(name: str, category: str, composition: str) -> str:
    """complex — если есть непустой состав, категория комплексная или название
    комплексное; иначе single. Ошибка здесь не страшна: kind не влияет на
    расчёт цен (там правит только COVERS), только на будущую аналитику."""
    comp = (composition or "").strip()
    if comp and comp != "-":
        return "complex"
    hay = f"{category or ''} {name or ''}"
    if _COMPLEX_CATEGORY_RX.search(hay):
        return "complex"
    return "single"


def parse_rows(payload: list[dict]) -> list[dict]:
    """Валидирует и нормализует записи скрейпа; битая запись — ValueError, не
    тихий пропуск (молчаливая потеря цены хуже упавшего импорта).

    external_code нормализуется до части до « / »: Юнилаб пишет
    «101 / A09.05.023» (код + номер номенклатуры) у синглов и голый код у
    комплексов; для ключа маппинга/уникальности нужен короткий стабильный код,
    полный номер живёт в url позиции."""
    rows = []
    for i, it in enumerate(payload):
        lab = str(it.get("lab_code") or "").strip()
        ext = str(it.get("external_code") or "").strip().split("/")[0].strip()
        name = str(it.get("name") or "").strip()
        price = it.get("price")
        scraped = str(it.get("scraped_at") or "").strip()
        if not lab or not ext or not name or price is None or not scraped:
            raise ValueError(f"запись #{i}: пустой lab_code/external_code/name/price/scraped_at: {it!r}")
        try:
            parsed_at = datetime.fromisoformat(scraped)
        except ValueError as e:
            raise ValueError(f"запись #{i} ({lab}/{ext}): bad scraped_at {scraped!r}") from e
        category = str(it.get("category") or "").strip() or None
        rows.append({
            "lab_key": lab, "lab_name": str(it.get("lab_name") or lab).strip() or lab,
            "city": str(it.get("city") or "Владивосток").strip() or "Владивосток",
            "external_code": ext, "name": name, "category": category,
            "kind": detect_kind(name, category, str(it.get("composition") or "")),
            "price_rub": round(float(price), 2),
            "currency": str(it.get("currency") or "RUB").strip() or "RUB",
            "turnaround": str(it.get("turnaround_time") or "").strip() or None,
            "url": str(it.get("url") or "").strip() or None,
            "composition": str(it.get("composition") or "").strip() or None,
            "parsed_at": parsed_at,
        })
    rows.sort(key=lambda r: (r["lab_key"], r["external_code"]))  # детерминизм апсерта
    return rows


def upsert(cur, rows: list[dict]) -> int:
    """card.lab + card.lab_item; повторный прогон по тому же прайсу — апдейт
    цены/даты, дублей нет. Возвращает число позиций."""
    from psycopg import sql

    from app.db import schema

    for lab_key in sorted({r["lab_key"] for r in rows}):
        r0 = next(r for r in rows if r["lab_key"] == lab_key)
        cur.execute(sql.SQL(
            "INSERT INTO {t} (key, name, city) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, city = EXCLUDED.city"
        ).format(t=sql.Identifier(schema(), "lab")),
            (lab_key, r0["lab_name"], r0["city"]))
    for r in rows:
        cur.execute(sql.SQL(
            "INSERT INTO {t} (lab_key, external_code, name, category, kind, price_rub, "
            "currency, turnaround, url, composition, parsed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (lab_key, external_code) DO UPDATE SET "
            "name = EXCLUDED.name, category = EXCLUDED.category, kind = EXCLUDED.kind, "
            "price_rub = EXCLUDED.price_rub, currency = EXCLUDED.currency, "
            "turnaround = EXCLUDED.turnaround, url = EXCLUDED.url, "
            "composition = EXCLUDED.composition, parsed_at = EXCLUDED.parsed_at"
        ).format(t=sql.Identifier(schema(), "lab_item")),
            (r["lab_key"], r["external_code"], r["name"], r["category"], r["kind"],
             r["price_rub"], r["currency"], r["turnaround"], r["url"],
             r["composition"], r["parsed_at"]))
    return len(rows)


def ingest_paths(paths: list[str]) -> int:
    from app.db import get_conn

    total = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        rows = parse_rows(payload)
        with get_conn() as conn, conn.cursor() as cur:
            total += upsert(cur, rows)
    return total


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def report(cur=None) -> None:
    """Незмапленные single-позиции с fuzzy-подсказками против каталога —
    человекочитаемая очередь приёмки маппинга (никогда не применяется
    автоматически)."""
    if cur is not None:
        _report(cur)
        return
    from app.db import get_conn

    with get_conn() as conn, conn.cursor() as c:
        _report(c)


def _report(cur) -> None:
    from psycopg import sql

    from app.db import schema

    cur.execute(sql.SQL("SELECT lab_key, external_code, name FROM {t} "
                        "WHERE kind = 'single' ORDER BY lab_key, external_code").format(
        t=sql.Identifier(schema(), "lab_item")))
    singles = cur.fetchall()  # до второго execute: он затирает результат первого
    catalog_names = {code: e["name"] for code, e in LAB_CATALOG.items()}
    cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE kind = 'complex'").format(
        t=sql.Identifier(schema(), "lab_item")))
    complex_total = cur.fetchone()[0]
    print(f"маппинг: {len(COVERS)} позиций; комплексов в прайсе: {complex_total} "
          f"(замаплены только стандартные наборы, чекапы — этап 3)")
    by_lab: dict[str, int] = {}
    for lab, ext, name in singles:
        if (lab, ext) in COVERS:
            continue
        norm = _norm(name)
        suggestions = []
        for code, cname in catalog_names.items():
            ratio = difflib.SequenceMatcher(None, norm, _norm(cname)).ratio()
            if ratio >= 0.45:
                suggestions.append((round(ratio, 2), code, cname))
        suggestions.sort(reverse=True)
        by_lab[lab] = by_lab.get(lab, 0) + 1
        hint = "; ".join(f"{c} {n} ({r})" for r, c, n in suggestions[:3]) or "—"
        print(f"  [{lab}] {ext} «{name}» → {hint}")
    for lab, n in sorted(by_lab.items()):
        print(f"итог [{lab}]: незмапленных single — {n}")
    # Ключи маппинга, для которых в текущем прайсе нет позиции: тихая потеря
    # покрытия (лаба сменила код/название или убрала позицию) — так же, как
    # «незмапленные», это очередь ручной приёмки, автоматически ничего не правим.
    cur.execute(sql.SQL("SELECT lab_key, external_code FROM {t}").format(
        t=sql.Identifier(schema(), "lab_item")))
    present = {(lab, ext) for lab, ext in cur.fetchall()}
    orphans = sorted(set(COVERS) - present)
    if orphans:
        by: dict[str, list[str]] = {}
        for lab, ext in orphans:
            by.setdefault(lab, []).append(ext)
        print(f"ключи маппинга без позиции в прайсе: {len(orphans)}")
        for lab, exts in sorted(by.items()):
            print(f"  [{lab}] {', '.join(exts[:20])}{' …' if len(exts) > 20 else ''}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "seed":
        if len(argv) < 2:
            print("usage: python -m app.lab_prices_ingest seed <file.json> [...]")
            return 2
        n = ingest_paths(argv[1:])
        print(f"импортировано позиций: {n}")
        return 0
    if argv[0] == "report":
        report()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
