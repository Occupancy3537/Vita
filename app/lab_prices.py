"""Цены лабораторий для панелей оптимизатора — этапы 0–1 плана
docs/PRICES_PLAN_QWEN.md (2026-09-29).

Что делает модуль:
  - load_items(cur) — прайс всех лаб из card.lab_item + покрытие кодами
    каталога из app/lab_prices_map.COVERS (маппинг живёт в коде, в БД его нет);
  - panel_offers(codes, items_by_lab) — ЧИСТАЯ функция: для готовой панели
    (набор кодов) и каждой лабы считает минимальное по стоимости покрытие
    панели позициями прайса этой лабы (set cover): одна позиция может закрывать
    много кодов (ОАК — ~28 строк каталога за одну цену), поэтому «сумма
    точечных цен» была бы враньём;
  - attach(cur, plan) — приклеивает labs[]/pick к панелям плана
    lab_optimizer.generate_plan(). lab_optimizer сам НЕ тронут: цены —
    надстройка над готовым составом панели и не могут двигать даты/пробирки
    (правило приоритетов: врач > даты/пробирки > цена).

Детерминизм — тот же принцип Части 2.8 lab_optimizer: sorted() с явным ключом
везде, тай-брейки решения по (цена, число позиций, кортеж кодов позиций).

Честность (ПЛАН СБОРКИ п.5 «пустое = отсутствует»):
  - кода нет ни в одной позиции лабы → он попадает в missing[] лабы
    («N анализов лаба не делает»), ничего не додумывается;
  - pick — самая дешёвая СРЕДИ ПОЛНОСТЬЮ покрывающих лаб; полных нет — самая
    дешёвая с дыркой (флаг missing в ответе говорит сам за себя);
  - parsed_at оффера = самая старая дата прайса среди выбранных позиций —
    дисклеймер «прайс от ДД.ММ» по худшей позиции, не по свежей.

Чистое ядро (min_cover/panel_offers) не импортирует БД — тестируется без
Postgres (tests/test_lab_prices.py), DB-слой импортирует app.db лениво."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.lab_catalog import LAB_CATALOG
from app.lab_prices_map import COVERS

logger = logging.getLogger(__name__)

# Легаси-компоненты ОАК, которые НИ ОДНА лаба не продаёт отдельной позицией:
# M047 «Цветовой показатель» — устаревший расчётный (современные анализаторы
# не выдают, MCHC уже в каталоге), M060 «Палочкоядерные %» — только ручной
# подсчёт диффа. Они остаются в плане и в export_text (это маркеры наблюдения),
# но из ЦЕНОВОЙ математики исключены — иначе каждая лаба вечна «с дыркой»,
# и честное правило «полные лабы — вперёд» никогда не срабатывает.
# 2026-09-30 (жалоба Влада: «красным горит, что лаборатория не делает кучу анализов — у всех лаб»):
# к легаси-компонентам добавлены ПОБОЧНЫЕ показатели ОАК (M050–M057: RDW-SD, PDW, MPV, PCT,
# P-LCR, P-LCC, незрелые гранулоциты) — они выдаются анализатором в составе ОАК (зависят от
# модели прибора), отдельной позиции в прайсах нет; и РАСЧЁТНЫЙ M014 «не-ЛПВП» (= общий
# холестерин − ЛПВП, оба заказываются). Такие маркеры остаются в плане (это показатели
# наблюдения), но из ценовой математики исключены и «дырой» лабы не считаются.
NOT_SEPARATELY_ORDERABLE = {"M047", "M060", "M050", "M051", "M052", "M053", "M054", "M055", "M056", "M057", "M014"}


@dataclass(frozen=True)
class LabItem:
    lab: str
    lab_name: str
    external_code: str
    name: str
    kind: str
    price_rub: float
    covers: tuple[str, ...]
    parsed_at: Optional[datetime]


def load_items(cur) -> dict[str, list[LabItem]]:
    """Прайс из card.lab_item + покрытие из COVERS. Позиции без маппинга
    возвращаются тоже (видны в отчётах), но в offers не участвуют."""
    from psycopg import sql

    from app.db import schema

    cur.execute(sql.SQL("SELECT key, name FROM {t} ORDER BY key").format(
        t=sql.Identifier(schema(), "lab")))
    lab_names = {k: n for k, n in cur.fetchall()}
    cur.execute(sql.SQL(
        "SELECT lab_key, external_code, name, kind, price_rub, parsed_at "
        "FROM {t} ORDER BY lab_key, external_code").format(
        t=sql.Identifier(schema(), "lab_item")))
    out: dict[str, list[LabItem]] = {}
    for lab, ext, name, kind, price, parsed_at in cur.fetchall():
        out.setdefault(lab, []).append(LabItem(
            lab=lab, lab_name=lab_names.get(lab, lab), external_code=ext,
            name=name, kind=kind, price_rub=float(price),
            covers=tuple(COVERS.get((lab, ext), ())), parsed_at=parsed_at))
    return out


def min_cover(codes: list[str], items: list[LabItem]) -> Optional[dict]:
    """Минимальное по стоимости покрытие codes позициями items одной лабы.

    Точный перебор с отсечением по цене (панель ≤ 12 кодов — DEFAULT_MAX_PER_DRAW,
    кандидатов мало). Код, который лаба не делает никакой позицией, уходит в
    missing — других дырок алгоритм не создаёт: дешёвое решение С дыркой при
    наличии полного никогда не выбирается (честное сравнение только между
    полными, см. докстринг модуля). None — если перекрытие с панелью пустое."""
    target = sorted(set(codes))
    candidates = sorted((it for it in items if it.covers), key=lambda it: it.external_code)
    if not any(set(it.covers) & set(target) for it in candidates):
        return None

    best: Optional[tuple] = None  # (cost, n_items, ext_codes_tuple, chosen, missing)

    def rec(remaining: list[str], chosen: list[LabItem], cost: float) -> None:
        nonlocal best
        if best is not None and cost > best[0]:
            return
        if not remaining:
            cand = (round(cost, 2), len(chosen),
                    tuple(it.external_code for it in sorted(chosen, key=lambda x: x.external_code)),
                    list(chosen), [])
            if best is None or cand[:3] < best[:3]:
                best = cand
            return
        c, rest = remaining[0], remaining[1:]
        covering = [it for it in candidates if c in it.covers]
        for it in covering:  # порядок задан сортировкой по external_code
            rec([x for x in rest if x not in it.covers], chosen + [it], cost + it.price_rub)
        if not covering:
            # лаба не делает этот маркер ничем — единственная ветка: дырка
            rec(rest, chosen, cost)

    rec(target, [], 0.0)
    if best is None:
        return None
    cost, _n, _codes, chosen, _ = best
    chosen_sorted = sorted(chosen, key=lambda it: it.external_code)
    covered_set: set[str] = set()
    for it in chosen_sorted:
        covered_set.update(it.covers)
    missing = [c for c in target if c not in covered_set]
    parsed = min((it.parsed_at for it in chosen_sorted if it.parsed_at), default=None)
    return {
        "cost": round(cost, 2),
        "items": chosen_sorted,
        "missing": missing,
        "parsed_at": parsed,
    }


def _missing_names(codes: list[str]) -> list[str]:
    return [LAB_CATALOG[c]["name"] if c in LAB_CATALOG else c for c in codes]


_RU_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _plural_ru(n: int) -> str:
    # копия lab_optimizer._plural_ru: импортировать его сюда нельзя — там
    # psycopg на уровне модуля, а ядро цен обязано тестироваться без БД
    if n % 10 == 1 and n % 100 != 11:
        return ""
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "а"
    return "ов"


def _ru_money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


# Цены собираются еженедельным скрейпом автоматически (scripts/lab_scrape) и
# НЕ проверяются человеком перед каждым использованием — честная метка в оффере
# и в экспорте. Сами строки прайса в БД несут verified-аналог в parsed_at и в
# исходном JSON (verified=false + verify_note); в БД поле не хранится — константа.
PRICES_VERIFIED = False

# Плата за взятие венозной крови (взрослая, венозная). Реальные значения с
# страниц/карточек позиций (2026-09-30): Гемотест «Вен. кровь (+230 ₽)» на
# карточке, Инвитро 280 ₽ (additional_services golk-API), ТАФИ «vzyatie-
# venoznoy-krovi» 300 ₽ (в прайсе), Юнилаб «Взятие крови +300 руб» на страницах.
# Обновляется скрейпом еженедельно (для будущего: скрейпер сохранит DRAW-BLOOD
# позицию; пока — константа из верифицированных данных).
DRAW_FEE_RUB = {"gemotest": 230.0, "invitro": 280.0, "tafi": 300.0, "unilab": 300.0}


def build_export_text(panel: dict, pick: dict) -> str:
    """Список ЗАКУПКИ для выбранной лаборатории: какие ПОЗИЦИИ прайса заказать
    (код лабы + название + цена + что закрывает), чтобы итог сошёлся с ценой
    оффера. Это не список маркеров (он и так на экране), а ровно то, что
    диктует оператору в лаборатории: комплексы не развёрнуты в синглы,
    синглы не слиты в комплексы. Пробирки — количеством, не типами (по макету)."""
    tubes = panel.get("tube_types") or []
    d_iso = panel.get("date") or ""
    try:
        from datetime import date as _d
        dd = _d.fromisoformat(d_iso)
        date_txt = f"{dd.day} {_RU_MONTHS[dd.month - 1]}"
    except ValueError:
        date_txt = d_iso
    n = pick.get("n") or panel.get("n_markers") or 0
    lines = [f"{date_txt} — панель из {n} анализ{_plural_ru(n)}, пробирки: {len(tubes)}"
             + (", натощак" if panel.get("fasting_required") else "") + "."]
    lines.append(f"{pick.get('name', '')}, {_ru_money(pick.get('price_rub') or 0)} ₽"
                 f" (прайс от {str(pick.get('parsed_at') or '')[:10]}):")
    for i, b in enumerate(pick.get("breakdown") or [], 1):
        covers = ", ".join(LAB_CATALOG[c]["name"] for c in b.get("covers", []) if c in LAB_CATALOG)
        note = f" [{b['note']}]" if b.get("note") else ""
        lines.append(f"{i}. {b.get('code')} {b.get('name')} — {_ru_money(b.get('price_rub') or 0)} ₽{note}")
        if covers:
            lines.append(f"   закрывает: {covers}")
    for m in pick.get("missing") or []:
        lines.append(f"«{m}» — {pick.get('name', 'лаборатория')} не делает, сдать в другой лаборатории.")
    draw_fee = DRAW_FEE_RUB.get(pick.get("key") or "")
    if draw_fee:
        lines.append(f"Взятие венозной крови — {_ru_money(draw_fee)} ₽ (включено в итог).")
    lines.append(f"Итого: {_ru_money(pick.get('price_rub') or 0)} ₽.")
    d_iso = str(pick.get("parsed_at") or "")[:10]
    if d_iso:
        lines.append(f"Цены собраны автоматически {d_iso[8:10]}.{d_iso[5:7]} — "
                     "проверьте на сайте лаборатории перед оплатой.")
    return "\n".join(lines)


def panel_offers(codes: list[str], items_by_lab: dict[str, list[LabItem]]) -> list[dict]:
    """Офферы всех лаб по готовой панели. Сортировка результата: полностью
    покрывающие по возрастанию цены (тай-брейк — key лабы), лабы с дырками
    после них. Первый элемент = pick.

    Не заказываемые отдельно маркеры (NOT_SEPARATELY_ORDERABLE) из ценовой
    математики исключены, но в covered ЗАСЧИТЫВАЮТСЯ как покрытые (денежно они
    бесплатны и идут в составе ОАК) — чтобы сводка «N из N» сходилась с числом
    маркеров панели, а missing содержал только реальные дыры лабы."""
    all_codes = sorted(set(codes))
    priceable = [c for c in all_codes if c not in NOT_SEPARATELY_ORDERABLE]
    if not priceable:
        return []
    offers = []
    for lab in sorted(items_by_lab):
        items = items_by_lab[lab]
        sol = min_cover(priceable, items)
        if sol is None:
            continue
        chosen = sol["items"]
        breakdown = []
        for it in chosen:
            in_panel = sorted(set(it.covers) & set(priceable))
            breakdown.append({
                "code": it.external_code, "name": it.name,
                "price_rub": round(it.price_rub, 2), "covers": in_panel,
                "extra": sorted(set(it.covers) - set(priceable)),  # «в комплекс входят ещё N — про запас»
                "note": None,  # заполняется ниже из NOTES
            })
        offers.append({
            "key": lab,
            "name": chosen[0].lab_name if chosen else lab,
            "price_rub": round(sol["cost"] + DRAW_FEE_RUB.get(lab, 0), 2),
            "covered": len(all_codes) - len(sol["missing"]),
            "n": len(all_codes),
            "missing": _missing_names(sol["missing"]),
            "breakdown": breakdown,
            "parsed_at": sol["parsed_at"].isoformat() if sol["parsed_at"] else None,
            "verified": PRICES_VERIFIED,
            "collected_at": sol["parsed_at"].isoformat() if sol["parsed_at"] else None,
            "draw_fee_rub": DRAW_FEE_RUB.get(lab),
            "cheapest": False,
        })
    if not offers:
        return []
    # сначала лаба с наименьшим числом дыр (полная — первая), потом дешевле, потом по ключу:
    # у больших панелей с ОАК-связкой полных лаб может не быть, тогда «самая дешёвая» не должна
    # побеждать лабу, закрывающую больше показателей
    offers.sort(key=lambda o: (len(o["missing"]), o["price_rub"], o["key"]))
    offers[0]["cheapest"] = True
    return offers


def attach(cur, plan: dict, chosen_lab: Optional[str] = None) -> None:
    """Добавляет labs[]/pick в каждую панель плана generate_plan(). До
    миграции 0005 (нет таблицы) просто пишет warning и ничего не меняет —
    цены не могут ломать «Врача»."""
    panels = plan.get("panels") or []
    if not panels:
        return
    try:
        items_by_lab = load_items(cur)
    except Exception as e:  # noqa: BLE001 — отсутствие таблицы/БД не должно ронять «Врача»
        logger.warning("lab prices unavailable, панели без цен: %s", e)
        return
    if not items_by_lab:
        return
    from app.lab_prices_map import NOTES

    for p in panels:
        codes = [m["code"] for m in (p.get("markers") or [])]
        offers = panel_offers(codes, items_by_lab)
        if not offers:
            continue
        for o in offers:
            for b in o["breakdown"]:
                b["note"] = NOTES.get((o["key"], b["code"]))
        pick = offers[0]
        if chosen_lab:
            pick = next((o for o in offers if o["key"] == chosen_lab), pick)
        p["labs"] = offers
        p["pick"] = pick
        # экспорт = список закупки выбранной лабы (коды позиций, комплексы,
        # итог); без цен остаётся прежний маркер-список из lab_optimizer
        p["export_text"] = build_export_text(p, pick)
