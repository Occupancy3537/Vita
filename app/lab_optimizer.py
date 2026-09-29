"""Движок оптимизации сдачи анализов (тикет «оптимизатор сдачи анализов»,
2026-09-26, Часть 2) — вход: каталог (app/lab_catalog.py) + история
(card.lab_result) + активные запросы (card.recommendation/card.intervention —
читаются, ЖИВЬЁМ, не дублируются; card.lab_request — только ручные
одноразовые запросы, см. докстринг migrations/0002_lab_request.sql). Выход —
план панелей на 6 месяцев вперёд.

Полностью детерминировано (Часть 2.8): при одинаковом состоянии БД два
вызова generate_plan() дают побайтово одинаковый JSON — никакого LLM внутри,
никаких set()/словарных обходов там, где порядок влияет на результат (везде
sorted() с явным ключом, включая тай-брейк по коду маркера).

Источники срока сдачи (due_date) для одного маркера, приоритет при совпадении
дат — ручной запрос > рекомендация > мониторинг интервенции > стоящее правило
каталога; при РАЗНЫХ датах побеждает БОЛЕЕ РАННЯЯ (Часть 1.2 «где нет
уверенности — консервативно», здесь — не дать сдвинуть нужный срок ПОЗЖЕ,
чем требует любой из источников):
  - «стоящее» (standing) — card/lab_catalog.LAB_CATALOG[code].default_interval_days
    не None: due = последний результат + интервал, либо СЕГОДНЯ, если
    результата не было ни разу (осознанно не переносим точные Next_Due старого
    health.lab_plan — они бы «замёрзли» и разошлись с этим же движком при
    следующем прогоне; интервал использован при заполнении каталога, сама
    дата — нет, см. докстринг lab_catalog.py).
  - «рекомендация» — активная card.recommendation, structured-сигнал
    (expectation.metric_key совпадает с кодом каталога) ИЛИ текстовый скан
    заголовка/повода (app.lab_catalog.find_markers_in_text +
    parse_relative_days) — только когда явно назван срок, не выдумываем его.
  - «мониторинг интервенции» — активная card.intervention, совпавшая с
    app.lab_catalog.INTERVENTION_MONITOR_RULES по названию.
  - «ручной запрос» — открытые строки card.lab_request (визит/консилиум/
    доктор, будущий путь через POST /labs/request).

Разовые (Часть 2.3): маркер с one_time=True и хотя бы одним историческим
результатом — не попадает в due_map никогда, независимо от источника."""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from psycopg import sql

from app.db import schema
from app.lab_catalog import (
    INTERVENTION_MONITOR_RULES,
    LAB_CATALOG,
    PHENOAGE_PANEL_MARKERS,
    find_markers_in_text,
    parse_relative_days,
)

logger = logging.getLogger(__name__)

GROUP_WINDOW_DAYS = 14  # Часть 2.5 — «через неделю» как пример, окно шире для реальной пользы
DEFAULT_MAX_PER_DRAW = 12  # Часть 2.4
OVERFLOW_PUSH_DAYS = 30  # «панель+2»: пауза перед следующим большим забором, не сразу следующий слот
_RU_MONTHS_GEN = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _ru_date(d: date) -> str:
    return f"{d.day} {_RU_MONTHS_GEN[d.month - 1]}"


@dataclass
class DueItem:
    code: str
    due_date: date
    source_type: str
    source_id: Optional[str]
    reason: str
    urgent: bool = False


_SOURCE_PRIORITY = {"manual": 4, "recommendation": 3, "intervention_monitor": 2, "standing": 1}


# ─────────────────────────── история (card.lab_result) ───────────────────────────

def _history(cur) -> tuple[dict[str, date], dict[str, int]]:
    """Последняя дата и число результатов по каждому маркеру — единственный
    вход из card.lab_result, не тронутый и не продублированный."""
    cur.execute(
        sql.SQL("SELECT marker_key, max(ts_event)::date, count(*) FROM {t} GROUP BY marker_key")
        .format(t=sql.Identifier(schema(), "lab_result"))
    )
    last_dates, counts = {}, {}
    for code, last, n in cur.fetchall():
        last_dates[code] = last
        counts[code] = n
    return last_dates, counts


# ─────────────────────────── источники due_date ───────────────────────────

def _standing_items(last_dates: dict, counts: dict, today: date) -> list[DueItem]:
    out = []
    for code, entry in LAB_CATALOG.items():
        if entry["one_time"]:
            continue  # разовые — только через историю, не через стоящее правило
        interval = entry["default_interval_days"]
        if interval is None:
            continue  # «по показаниям» — не заводим расписание сами
        last = last_dates.get(code)
        due = (last + timedelta(days=interval)) if last else today
        out.append(DueItem(code, due, "standing", None, entry["purpose"]))
    return out


def _requests_from_recommendations(cur, today: date) -> list[DueItem]:
    cur.execute(
        sql.SQL(
            "SELECT r.id, r.title, r.action, r.rationale, r.started_ts, r.ts_recorded, "
            "e.metric_key, e.window_days, e.lag_days "
            "FROM {rc} r LEFT JOIN {ex} e ON e.rec_id = r.id AND e.role = 'primary' "
            "WHERE r.status IN ('proposed', 'active')"
        ).format(rc=sql.Identifier(schema(), "recommendation"), ex=sql.Identifier(schema(), "expectation"))
    )
    out = []
    for rec_id, title, action, rationale, started_ts, ts_recorded, metric_key, window_days, lag_days in cur.fetchall():
        anchor = (started_ts or ts_recorded).date()
        # структурный сигнал — expectation.metric_key прямо совпал с кодом каталога
        if metric_key and metric_key in LAB_CATALOG:
            days = (window_days or 0) + (lag_days or 0) or 90
            out.append(DueItem(metric_key, anchor + timedelta(days=days), "recommendation", rec_id,
                                title or rationale or "рекомендация"))
            continue
        # текстовый скан — Часть 1.3, повод не выдуман, срок явно назван в тексте
        text = " ".join(p for p in (title, action, rationale) if p)
        days = parse_relative_days(text)
        if days is None:
            continue
        for code in find_markers_in_text(text):
            out.append(DueItem(code, anchor + timedelta(days=days), "recommendation", rec_id, title or text))
    return out


def _requests_from_interventions(cur, today: date) -> list[DueItem]:
    cur.execute(
        sql.SQL("SELECT id, name, started_ts FROM {t} WHERE status = 'active'")
        .format(t=sql.Identifier(schema(), "intervention"))
    )
    out = []
    for iv_id, name, started_ts in cur.fetchall():
        if not started_ts:
            continue
        for rule in INTERVENTION_MONITOR_RULES:
            if rule["pattern"].search(name or ""):
                due = started_ts.date() + timedelta(days=rule["days_from_start"])
                for code in rule["markers"]:
                    out.append(DueItem(code, due, "intervention_monitor", iv_id, rule["reason"]))
    return out


def _requests_manual(cur) -> list[DueItem]:
    cur.execute(
        sql.SQL("SELECT marker_code, due_date, source_type, source_id, reason, urgent, requested_ts "
                "FROM {t} WHERE status = 'open'").format(t=sql.Identifier(schema(), "lab_request"))
    )
    out = []
    for code, due, src_type, src_id, reason, urgent, requested_ts in cur.fetchall():
        out.append(DueItem(code, due or requested_ts.date(), "manual", src_id, reason or src_type, bool(urgent)))
    return out


def mark_fulfilled(cur, marker_codes: list[str]) -> int:
    """Часть 3.1 — новый лабораторный результат закрывает открытые РУЧНЫЕ
    запросы (card.lab_request) на эти же коды. Не трогает стоящие правила
    каталога/рекомендации/интервенции — те читаются живьём из card.lab_result
    при каждом расчёте плана, отдельной пометки не требуют (см. докстринг
    модуля). Возвращает число закрытых строк — для теста/лога, не для
    ответа Владу (это внутренний housekeeping)."""
    if not marker_codes:
        return 0
    cur.execute(
        sql.SQL("UPDATE {t} SET status = 'fulfilled' WHERE status = 'open' AND marker_code = ANY(%s)")
        .format(t=sql.Identifier(schema(), "lab_request")),
        (marker_codes,),
    )
    return cur.rowcount


def _merge_due(*groups: list[DueItem], one_time_seen: set[str]) -> dict[str, DueItem]:
    """Часть 2.3 — разовые с уже существующим результатом не попадают никуда,
    из ЛЮБОГО источника. При конкуренции за один код — более ранняя дата
    побеждает; при равенстве дат — источник с более высоким приоритетом
    (см. _SOURCE_PRIORITY), тай-брейк детерминирован."""
    best: dict[str, DueItem] = {}
    for group in groups:
        for item in group:
            entry = LAB_CATALOG.get(item.code)
            if entry and entry["one_time"] and item.code in one_time_seen:
                continue
            cur_best = best.get(item.code)
            if cur_best is None:
                best[item.code] = item
                continue
            if item.due_date < cur_best.due_date:
                best[item.code] = item
            elif item.due_date == cur_best.due_date:
                if _SOURCE_PRIORITY[item.source_type] > _SOURCE_PRIORITY[cur_best.source_type]:
                    best[item.code] = item
    return best


# ─────────────────────────── группировка в панели ───────────────────────────

def _build_panels(due_items: list[DueItem], today: date, horizon_days: int,
                   max_per_draw: int, group_window_days: int) -> tuple[list[dict], list[dict], list[dict]]:
    horizon_end = today + timedelta(days=horizon_days)
    items = sorted((it for it in due_items if it.due_date <= horizon_end),
                    key=lambda it: (it.due_date, it.code))

    conflicts = []
    raw_panels: list[tuple[date, list[DueItem]]] = []

    urgent = [it for it in items if it.urgent]
    normal = [it for it in items if not it.urgent]

    # Панель PhenoAge АТОМАРНА (правило одного дня, Влад 2026-09-29): формула
    # Levine честна только по крови одного забора, «дособирать» маркеры с
    # разных дат бессмысленно. Все 9 стоящих PhenoAge-маркеров едут одним
    # забором с самым ранним сроком среди них (псевдо-элемент разворачивается
    # в состав группы ниже) и никогда не подрезаются лимитом на забор.
    pheno_set = frozenset(PHENOAGE_PANEL_MARKERS)
    pheno_unit = [it for it in normal if it.code in pheno_set]
    if pheno_unit:
        normal = [it for it in normal if it.code not in pheno_set]
        normal.append(DueItem("__phenoage__", min(it.due_date for it in pheno_unit),
                              "standing", None, "панель PhenoAge одним забором"))
        normal.sort(key=lambda it: (it.due_date, it.code))

    for it in urgent:
        raw_panels.append((max(it.due_date, today), [it]))
        conflicts.append({
            "code": it.code, "name": LAB_CATALOG.get(it.code, {}).get("name", it.code),
            "due_date": it.due_date.isoformat(),
            "reason": f"{it.reason} — отмечено срочным врачом, показано отдельно, не объединено ради экономии заборов",
        })

    remaining = list(normal)
    while remaining:
        anchor = remaining[0]
        anchor_effective = max(anchor.due_date, today)
        window_end = anchor_effective + timedelta(days=group_window_days)
        group, rest = [], []
        for it in remaining:
            if it is anchor:
                group.append(it)
                continue
            it_effective = max(it.due_date, today)
            # Уже просроченный анализ (due_date <= today) объединяем с любым
            # другим «сейчас»-забором без оглядки на его интервал — это не
            # ПЕРЕНОС РАНЬШЕ срока (Часть 2.5 про такой перенос и просит
            # интервал ≥90), тест и так уже пора сдавать. Ограничение по
            # интервалу применяется, только когда мы тянем ЕЩЁ НЕ наступивший
            # срок НАЗАД, чтобы успеть в более раннюю панель.
            already_due = it.due_date <= today
            entry = LAB_CATALOG.get(it.code, {})
            interval = entry.get("default_interval_days")
            shiftable = already_due or entry.get("one_time") or interval is None or interval >= 90
            if it_effective <= window_end and shiftable:
                group.append(it)
            else:
                rest.append(it)
        remaining = rest
        panel_date = anchor_effective

        if "__phenoage__" in {it.code for it in group}:
            group = [it for it in group if it.code != "__phenoage__"] + pheno_unit
        if len(group) > max_per_draw:
            # подрезка не выкидывает PhenoAge-маркеры (один забор — не договорённость)
            group.sort(key=lambda it: (it.code not in pheno_set, it.due_date, it.code))
            overflow = group[max_per_draw:]
            group = group[:max_per_draw]
            push_date = panel_date + timedelta(days=OVERFLOW_PUSH_DAYS)
            remaining.extend(DueItem(it.code, push_date, it.source_type, it.source_id, it.reason) for it in overflow)
            remaining.sort(key=lambda it: (it.due_date, it.code))

        raw_panels.append((panel_date, group))

    # Часть 2.4/2.7: лимит на забор + большой бэклог просроченных анализов
    # может растянуть очередь панелей ЗА пределы заявленного полугодового
    # горизонта (панель+2 после панели+2 после...) — не разрешаем этому
    # молча "прорасти" за горизонт незамеченным: то, что не поместилось,
    # выносится отдельным честным списком, а не тихой лишней панелью.
    raw_panels.sort(key=lambda p: (p[0], p[1][0].code if p[1] else ""))
    in_horizon = [(d, g) for d, g in raw_panels if d <= horizon_end]
    beyond = [(d, g) for d, g in raw_panels if d > horizon_end]
    panels = [_render_panel(i + 1, d, g) for i, (d, g) in enumerate(in_horizon)]
    beyond_horizon = [
        {"code": it.code, "name": LAB_CATALOG.get(it.code, {}).get("name", it.code),
         "would_be_date": d.isoformat(),
         "reason": "не поместилось в горизонт при текущем лимите на забор — увеличь лимит или начни раньше"}
        for d, g in beyond for it in g
    ]
    return panels, conflicts, beyond_horizon


def _render_panel(panel_no: int, panel_date: date, members: list[DueItem]) -> dict:
    members = sorted(members, key=lambda it: it.code)
    markers = []
    tube_types: set[str] = set()
    fasting = False
    total_price = 0.0
    price_known = True
    shifted = []
    for it in members:
        entry = LAB_CATALOG.get(it.code, {})
        why = entry.get("purpose", "")
        if it.reason and it.reason != why:
            why = f"{why} — {it.reason}" if why else it.reason
        if entry.get("tube_type"):
            tube_types.add(entry["tube_type"])
        if entry.get("fasting_required"):
            fasting = True
        price = entry.get("price_rub")
        if price is None:
            price_known = False
        else:
            total_price += price
        markers.append({
            "code": it.code, "name": entry.get("name", it.code), "category": entry.get("category"),
            "why": why, "source_type": it.source_type, "source_id": it.source_id,
            "fasting_required": bool(entry.get("fasting_required")), "price_rub": price,
            "confidence": entry.get("confidence"), "notes": entry.get("notes"),
            "natural_due_date": it.due_date.isoformat(),
        })
        shift = (panel_date - it.due_date).days
        if shift != 0:
            shifted.append({"code": it.code, "name": entry.get("name", it.code),
                             "natural_due_date": it.due_date.isoformat(), "shifted_to": panel_date.isoformat(),
                             "shift_days": shift})

    n = len(markers)
    tubes_txt = ", ".join(sorted(tube_types)) if tube_types else "уточнить в лаборатории"
    lines = [f"{_ru_date(panel_date)} — панель из {n} анализ{_plural_ru(n)}, пробирки: {tubes_txt}"
             + (", натощак" if fasting else "") + "."]
    for m in markers:
        lines.append(f"- {m['name']}" + (" (натощак)" if m["fasting_required"] else ""))
    export_text = "\n".join(lines)

    return {
        "panel_no": panel_no, "date": panel_date.isoformat(), "n_markers": n,
        "markers": markers, "fasting_required": fasting, "tube_types": sorted(tube_types),
        "total_price_rub": round(total_price, 2) if (price_known and total_price) else None,
        "shifted": shifted, "export_text": export_text,
    }


def _plural_ru(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return ""
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "а"
    return "ов"


# ─────────────────────────── публичный вход ───────────────────────────

def generate_plan(cur, today: Optional[date] = None, horizon_days: int = 180,
                   max_per_draw: int = DEFAULT_MAX_PER_DRAW,
                   group_window_days: int = GROUP_WINDOW_DAYS) -> dict:
    from app.timeutil import now_local
    today = today or now_local().date()

    last_dates, counts = _history(cur)
    one_time_seen = {code for code, entry in LAB_CATALOG.items() if entry["one_time"] and counts.get(code, 0) > 0}

    standing = _standing_items(last_dates, counts, today)
    from_rec = _requests_from_recommendations(cur, today)
    from_iv = _requests_from_interventions(cur, today)
    from_manual = _requests_manual(cur)

    due_map = _merge_due(from_manual, from_rec, from_iv, standing, one_time_seen=one_time_seen)
    due_items = sorted(due_map.values(), key=lambda it: (it.due_date, it.code))

    panels, conflicts, beyond_horizon = _build_panels(
        due_items, today, horizon_days, max_per_draw, group_window_days)

    return {
        "generated_at": today.isoformat(),
        "horizon_end": (today + timedelta(days=horizon_days)).isoformat(),
        "panels": panels,
        "conflicts": conflicts,
        "beyond_horizon": beyond_horizon,
        "n_markers_planned": len(due_items),
    }
