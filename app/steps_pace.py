# -*- coding: utf-8 -*-
"""Темп дня по шагам (2026-10-01): круг «Движение» перестаёт быть вечной сотней и показывает, успеваешь ли
сегодня. Ожидаемые шаги к этому часу = цель × доля дня (линейно 07:00–22:00). Проверено на 59 днях реального
движения Garmin: личная кривая отличается от линейной не больше чем на 0,05 доли дня — отдельной кривой не нужно.

pace_score = шаги ÷ ожидаемые к этому часу, не больше 100. До ~08:00 ожидаемое меньше 3% цели — оценка не имеет
смысла, держим 100. К 22:00 ожидаемое = цель, и оценка = процент выполнения цели."""
from typing import Optional

from app import goals

DAY_START, DAY_END = 7.0, 22.0
MIN_EXPECTED_SHARE = 0.03        # раньше этого ожидаемого — оценку не считаем (держим 100)
STEPS_PER_MIN_WALK = 100         # средний темп ходьбы, шагов/мин — для «≈ N мин ходьбы»
AT_RISK_AFTER_HOUR = 15.0
AT_RISK_BELOW_PACE = 0.85


def expected_fraction(hour: float) -> float:
    return max(0.0, min(1.0, (hour - DAY_START) / (DAY_END - DAY_START)))


def expected_steps(hour: float, target: Optional[int] = None) -> int:
    target = target or goals.steps_target()
    return round(target * expected_fraction(hour))


def pace(steps: Optional[int], hour: float, target: Optional[int] = None) -> dict:
    """{expected, delta, score, behind, to_pace_min, to_goal_min, left}. steps=None -> все None."""
    target = target or goals.steps_target()
    if steps is None:
        return {"expected": None, "delta": None, "score": None, "behind": False,
                "to_pace_min": None, "to_goal_min": None, "left": None}
    exp = expected_steps(hour, target)
    score = 100 if exp < target * MIN_EXPECTED_SHARE else min(100, round(100 * steps / exp))
    delta = steps - exp
    left = max(0, target - steps)
    return {"expected": exp, "delta": delta, "score": score,
            "behind": exp >= target * MIN_EXPECTED_SHARE and steps < exp * AT_RISK_BELOW_PACE,
            "to_pace_min": max(0, round(-delta / STEPS_PER_MIN_WALK)) if delta < 0 else 0,
            "to_goal_min": round(left / STEPS_PER_MIN_WALK), "left": left}


def closed_day_score(steps: Optional[int], target: Optional[int] = None) -> Optional[int]:
    """Закрытый день: процент выполнения цели, не больше 100."""
    target = target or goals.steps_target()
    return None if steps is None else min(100, round(100 * steps / target))
