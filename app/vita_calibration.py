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

_fetch_history()/_historical_day_dicts() — общая реконструкция "{today,health}
для произвольного дня истории" — живёт в app/vita.py (не здесь): её же
использует app.vita.write_day_snapshot() (снимок дня, Часть «Бэкенд-дельта»
п.4) — один источник правды для "что мы вообще можем честно узнать про
прошлый день", не два похожих, но разных."""
from datetime import timedelta

from app.vita import (
    _collect_judgments,
    _fetch_history,
    _historical_day_dicts,
    _score_from_judgments,
    _score_with_segment_fixed,
)

CALIBRATION_THRESHOLD_POINTS = 5  # Часть 2 тикета — гейт на выпуск, не косметика


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
            today_d, health_d = _historical_day_dicts(rows_tuple, rows_flat, idx, meals, targets)
            today_next, health_next = _historical_day_dicts(rows_tuple, rows_flat, idx + 1, meals, targets)
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
