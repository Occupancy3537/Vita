"""Медпаспорт на вынос (стратегический разбор 2026-09-23 — "лучший ROI,
почти бесплатно из существующих данных"): одностраничная выжимка для показа
живому врачу на приёме — аллергии, хронические ограничения, активные
лекарства, последние лабы с динамикой. Ни одного нового источника данных:
активные лекарства/лабы вне референса/последние заметки врача — те же
частные функции, что уже собирает досье доктора (app.doctor.context,
build_dossier) для каждого хода разговора; профиль/аллергии/хроника —
health.user_profile (то же поле "ОДА и неврология", что уже читает
patient_gate.py для гейта нагрузки); активные ограничения — health.patient_state
(тот же источник, что и гейт на дашборде).

Осознанно НЕ PDF (решение Влада 2026-09-23): живая HTML-страница, тот же
принцип, что у остального дашборда — токен в URL, всегда актуальные данные,
ноль новых зависимостей. Распечатать/сохранить в PDF — штатная функция
браузера, если понадобится физическая копия."""
from typing import Optional

from app.doctor.context import _active_meds, _labs_out_of_range, _recent_doctor_notes


def _profile(cur) -> dict:
    cur.execute(
        'SELECT "User_ID", "Year of birth", "Gender", "Height, sm", "Weight, kg", '
        '"Allergies", "Chronic_Conditions" FROM health.user_profile LIMIT 1'
    )
    row = cur.fetchone()
    if row is None:
        return {}
    uid, byear, gender, height, weight, allergies, chronic = row
    return {
        "name": uid, "birth_year": byear, "gender": gender,
        "height_cm": height, "weight_kg": weight,
        "allergies": allergies or None, "chronic_conditions": chronic or None,
    }


def _active_conditions(cur) -> list[dict]:
    """health.patient_state — активные ограничения по здоровью (тот же
    источник, что читает patient_gate.load_gate для гейта нагрузки на
    дашборде): диагноз, что нельзя, что можно, когда пересмотр."""
    cur.execute(
        'SELECT "Condition", "Contra_Load", "Allowed", "Provokers", "Confirmed_Date", '
        '"Review_Due", "Source" FROM health.patient_state '
        "WHERE lower(\"Status\") = 'active' ORDER BY \"Confirmed_Date\" DESC NULLS LAST"
    )
    return [
        {"condition": c, "contra": contra, "allowed": allowed, "provokers": prov,
         "confirmed_date": str(d) if d else None, "review_due": str(rd) if rd else None, "source": src}
        for c, contra, allowed, prov, d, rd, src in cur.fetchall()
    ]


def _recent_labs(cur, limit: int = 15) -> list[dict]:
    """Последняя запись по каждому маркеру (card.lab_result — канон,
    см. registrar.py) + предыдущая, для стрелки динамики. Полная свежая
    картина, не только вне референса — врачу на приёме нужнее весь срез."""
    from app.db import schema
    from psycopg import sql
    cur.execute(
        sql.SQL(
            "SELECT marker_key, marker_label, value_num, unit, ref_min, ref_max, ts_event "
            "FROM {t} WHERE value_num IS NOT NULL ORDER BY marker_key, ts_event DESC"
        ).format(t=sql.Identifier(schema(), "lab_result"))
    )
    by_marker: dict[str, list] = {}
    for key, label, value, unit, lo, hi, ts in cur.fetchall():
        by_marker.setdefault(key, []).append((label, value, unit, lo, hi, ts))

    out = []
    for key, entries in by_marker.items():
        label, value, unit, lo, hi, ts = entries[0]
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi) if hi is not None else None
        val_f = float(value)
        out_of_range = (lo_f is not None and val_f < lo_f) or (hi_f is not None and val_f > hi_f)
        prev_value: Optional[float] = float(entries[1][1]) if len(entries) > 1 else None
        out.append({
            "marker": label or key, "value": val_f, "unit": unit,
            "ref_min": lo_f, "ref_max": hi_f, "date": str(ts)[:10],
            "out_of_range": out_of_range, "prev_value": prev_value,
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:limit]


def build_medpassport(cur) -> dict:
    return {
        "profile": _profile(cur),
        "active_conditions": _active_conditions(cur),
        "active_meds": _active_meds(cur),
        "recent_labs": _recent_labs(cur),
        "labs_out_of_range": _labs_out_of_range(cur, limit=20),
        "recent_doctor_notes": _recent_doctor_notes(cur, limit=5),
    }
