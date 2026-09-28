"""Калибровка ring.ahead на истории (Vita v2, этап 1, тикет 2026-09-28,
Часть «Бэкенд-дельта» п.2): «прогнозы по прошлым дням vs факты, показать
расхождение. Систематически врёт >5 очков — порог калибровки, не выпуск.»

Метод (единственный, который честно проверяем на данных, которые реально
есть): для каждого закрытого дня D с хотя бы одним "bad"/"warn" суждением в
сегменте, который стал бы главным действием (food приоритетнее move, тот же
порядок, что app.vita._main_action_segment/build_nudge) — считаем ahead_D
(тот же сегмент виртуально "исправлен"). Если этот же сегмент ДЕЙСТВИТЕЛЬНО
стал хорошим на следующий день D+1 (человек так и сделал/само наладилось) —
сравниваем ahead_D с РЕАЛЬНЫМ overall-скором D+1.

Это не идеальный контролируемый эксперимент (остальные критерии D+1 тоже
меняются день ото дня — сон, ВСР и т.п. не подчиняются действию по еде/шагам)
— это и есть "показать расхождение", а не "доказать точность до балла".
Переиспользует ТЕ ЖЕ функции, что и живой путь (app/dashboard._build_reasons,
_baseline_metrics_for_index, _budget_for_day; app/vita._collect_judgments,
_score_from_judgments, _score_with_segment_fixed) — не second-guess той же
логики другими порогами.

Две формы истории нужны параллельно (наследие двух разных функций
dashboard.py, которые исторически читали health.daily_trends по-разному —
не унифицировано в этом тикете, не тот объём): `_baseline`/`_load_mean`
(app/dashboard.py, для decision.reasons) ждут list[dict] с "Дата" СТРОКОЙ
внутри каждого словаря; `_baseline_for`/`_baseline_metrics_for_index` (для
health.metrics) ждут list[tuple[date, dict]] с датой ОБЪЕКТОМ на первом
месте. Обе строятся здесь из одного SQL-запроса, каждая — под свою функцию."""
from datetime import timedelta

from psycopg.rows import dict_row

from app.dashboard import (
    _baseline,
    _baseline_metrics_for_index,
    _budget_for_day,
    _build_reasons,
    _load_mean,
    _num,
)
from app.vita import (
    _collect_judgments,
    _score_from_judgments,
    _score_with_segment_fixed,
)

CALIBRATION_THRESHOLD_POINTS = 5  # Часть 2 тикета — гейт на выпуск, не косметика

_DAILY_TRENDS_COLS = [
    "Дата", "Восстановление_BodyBattery", "ВСР_ночная", "ACWR_Garmin", "ACWR_Status",
    "Тренировка_Ккал", "Чистый_сон_мин", "Оценка_сна_балл", "Эффективность_сна_",
    "Стресс_дневной_средний", "Шаги_за_вчера", "Пульс_ночной_средний", "VO2_Max",
]


def _fetch_history(cur):
    cols = ", ".join(f'"{c}"' for c in _DAILY_TRENDS_COLS)
    cur.execute(f'SELECT {cols} FROM health.daily_trends ORDER BY "Дата" ASC')
    raw = [dict(zip(_DAILY_TRENDS_COLS, r)) for r in cur.fetchall()]
    raw = [r for r in raw if r["Дата"]]

    # _baseline_for/_baseline_metrics_for_index (портировано из get_health_dashboard)
    # НЕ конвертирует значения сами — ждут уже готовые числа в rows[i][1][col],
    # ровно как get_health_dashboard строит их через _num() при чтении. health.*
    # колонки — TEXT (наследие Sheets), сырые значения из курсора — строки.
    rows_tuple = [(r["Дата"], {k: _num(v) for k, v in r.items() if k != "Дата"}) for r in raw]
    # _baseline/_load_mean (портировано из get_today_dashboard), наоборот,
    # сами вызывают _num() на каждое значение — им годится и сырая строка.
    rows_flat = [{**r, "Дата": r["Дата"].isoformat()} for r in raw]

    cur.execute(
        'SELECT "Date", "Насыщенные жиры", "Натрий", "Добавленный сахар" '
        'FROM health.meals WHERE "Date" IS NOT NULL'
    )
    meals = [{"Date": d.isoformat() if hasattr(d, "isoformat") else str(d),
              "Насыщенные жиры": fat, "Натрий": na, "Добавленный сахар": sugar}
             for d, fat, na, sugar in cur.fetchall()]

    targets_cur = cur.connection.cursor(row_factory=dict_row)
    targets_cur.execute('SELECT * FROM health.nutrient_targets')
    targets = targets_cur.fetchall()

    return rows_tuple, rows_flat, meals, targets


def _reconstruct_day(rows_tuple: list, rows_flat: list[dict], idx: int,
                      meals: list[dict], targets: list[dict]) -> tuple[dict, dict]:
    """{today, health}-совместимые словари для дня rows_tuple[idx], теми же
    формулами, что get_today_dashboard/get_health_dashboard, для
    произвольного индекса истории, не только последнего дня."""
    day, row = rows_tuple[idx]
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]

    bb = _num(row.get("Восстановление_BodyBattery"))
    hrv = _num(row.get("ВСР_ночная"))
    hrv_base = _baseline(rows_flat, "ВСР_ночная", day_iso, 30)
    hrv_delta = (hrv - hrv_base) if (hrv is not None and hrv_base is not None) else None

    acwr_garmin = _num(row.get("ACWR_Garmin"))
    acwr_status_g = (str(row.get("ACWR_Status") or "").strip().upper()) or None
    if acwr_garmin is not None:
        acwr, acwr_source = acwr_garmin, "garmin"
    else:
        acute, chronic = _load_mean(rows_flat, day_iso, 7), _load_mean(rows_flat, day_iso, 28)
        acwr = round((acute / chronic) * 100) / 100 if (acute is not None and chronic) else None
        acwr_source = "self" if acwr is not None else None
    load_high = (acwr_status_g == "HIGH") if acwr_status_g else (acwr is not None and acwr > 1.5)

    reasons = _build_reasons(bb, hrv_delta, acwr, acwr_status_g, acwr_source, load_high)
    metric_by_key = _baseline_metrics_for_index(rows_tuple, idx)
    budget = _budget_for_day(meals, targets, day_iso)

    today = {"decision": {"reasons": reasons}, "budget": budget}
    health = {"metrics": list(metric_by_key.values())}
    return today, health


def historical_ahead_samples(cur, days_back: int = 180) -> list[dict]:
    """Возвращает список {date, segment, ahead, actual_next, error} — только
    дни, где главное действие имело смысл (food/move в bad/warn) И этот
    сегмент действительно стал хорошим на следующий день."""
    rows_tuple, rows_flat, meals, targets = _fetch_history(cur)
    if len(rows_tuple) < 40:  # нужна база минимум на _baseline (30д) + пара дней сравнения
        return []

    last_day = rows_tuple[-1][0]
    cutoff = (last_day - timedelta(days=days_back)) if hasattr(last_day, "__sub__") else None

    samples = []
    start_idx = max(30, len(rows_tuple) - days_back)
    for idx in range(start_idx, len(rows_tuple) - 1):
        day, _ = rows_tuple[idx]
        if cutoff and day < cutoff:
            continue
        try:
            today_d, health_d = _reconstruct_day(rows_tuple, rows_flat, idx, meals, targets)
            today_next, health_next = _reconstruct_day(rows_tuple, rows_flat, idx + 1, meals, targets)
        except Exception:
            continue

        crit_d = _collect_judgments(today_d, health_d)
        current_d = _score_from_judgments(crit_d["overall"])
        if current_d is None:
            continue

        # _main_action_segment() (app/vita.py) читает live-состояние (часы/
        # еда сегодня/время суток) и белковую цель "на сейчас" — ни того, ни
        # другого у прошлого дня нет и не может быть реконструировано (не то,
        # что тикет просит калибровать). Тот же ПРИОРИТЕТ, что и там (food
        # раньше move), решаем прямо по историческим сегментам:
        segment = None
        if crit_d["food"] and ("bad" in crit_d["food"] or "warn" in crit_d["food"]):
            segment = "food"
        elif crit_d["move"] and ("bad" in crit_d["move"] or "warn" in crit_d["move"]):
            segment = "move"
        if segment is None:
            continue

        ahead = _score_with_segment_fixed(crit_d, segment)

        crit_next = _collect_judgments(today_next, health_next)
        seg_next = crit_next.get(segment) or []
        became_good = seg_next and all(j == "good" for j in seg_next)
        if not became_good:
            continue
        actual_next = _score_from_judgments(crit_next["overall"])
        if actual_next is None or ahead is None:
            continue

        samples.append({
            "date": day.isoformat() if hasattr(day, "isoformat") else str(day),
            "segment": segment, "ahead": ahead, "actual_next": actual_next,
            "error": ahead - actual_next,
        })
    return samples


def calibration_report(cur, days_back: int = 180) -> dict:
    samples = historical_ahead_samples(cur, days_back)
    if not samples:
        return {"n": 0, "mean_abs_error": None, "ok": None,
                "note": "недостаточно исторических пар (сегмент действительно исправился на следующий день) — ahead не проверен на данных"}
    mean_abs_error = sum(abs(s["error"]) for s in samples) / len(samples)
    return {
        "n": len(samples), "mean_abs_error": round(mean_abs_error, 2),
        "ok": mean_abs_error <= CALIBRATION_THRESHOLD_POINTS,
        "samples": samples,
    }
