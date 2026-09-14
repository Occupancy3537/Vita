"""
П5 §5 — слой C: метрические правила над fact-потоком устройства. Baseline —
персональный (self-vs-self, trailing median), не популяционный (Д1) — у человека
с RHR 46 скачок до 54 значим, у человека с RHR 60 — шум.

Честно об объёме (§5.2, зафиксировано самой спекой): слой работает на том, что
наблюдается (Garmin-телеметрия) — не претендует на кардио-неотложность, только на
"стоит проверить" (L1, никогда выше — осознанное ограничение слоя, не недоделка).
Реализованы 2 стартовых правила из примеров спеки (rf-c-01, rf-c-02); rf-c-03
(cross-правило со слоем B) — на этом же принципе, добавляется когда появится
реальный B-флаг того же дня для проверки, не реализовано отдельным заходом.
"""
import statistics
from collections import OrderedDict
from typing import Optional

from psycopg import sql

from app.db import schema


def _daily_series(cur, metric_key: str, days: int = 14) -> "OrderedDict[object, float]":
    """Последнее значение метрики по каждому календарному дню за окно, по порядку."""
    cur.execute(
        sql.SQL(
            "SELECT date(ts_event) d, value_num FROM {t} "
            "WHERE metric_key = %s AND value_num IS NOT NULL "
            "AND ts_event >= now() - (%s || ' days')::interval "
            "ORDER BY ts_event"
        ).format(t=sql.Identifier(schema(), "fact")),
        (metric_key, days),
    )
    by_day: "OrderedDict[object, float]" = OrderedDict()
    for d, v in cur.fetchall():
        by_day[d] = float(v)  # позже в порядке ts_event -> последнее значение дня остаётся
    return by_day


def check_infectious_pattern(cur) -> Optional[dict]:
    """rf-c-01: rhr >= trailing_median+7 (>=2 дня) ∧ stress>45 (>=2 дня) ∧ sleep_min<360 (>=2 ночи)."""
    rhr = _daily_series(cur, "rhr")
    if len(rhr) < 9:
        return None
    days = list(rhr.keys())[-9:]
    baseline_days, check_days = days[:-2], days[-2:]
    baseline = statistics.median(rhr[d] for d in baseline_days)

    stress = _daily_series(cur, "stress")
    sleep = _daily_series(cur, "sleep_min")

    rhr_hit = all(rhr.get(d, float("-inf")) >= baseline + 7 for d in check_days)
    stress_hit = all(d in stress and stress[d] > 45 for d in check_days)
    sleep_hit = all(d in sleep and sleep[d] < 360 for d in check_days)

    if rhr_hit and stress_hit and sleep_hit:
        return {
            "rule": "rf-c-01", "category": "systemic_warning", "level": "L1",
            "message": "похоже на начало инфекции — проверься, отложи нагрузку",
            "trace": {"baseline_rhr": round(baseline, 1), "check_days": [str(d) for d in check_days],
                      "rhr": [rhr[d] for d in check_days], "stress": [stress[d] for d in check_days],
                      "sleep_min": [sleep[d] for d in check_days]},
        }
    return None


def check_recovery_collapse(cur) -> Optional[dict]:
    """rf-c-02: hrv <= baseline*0.7 (>=3 дня) ∧ rhr >= baseline+5 (>=3 дня) ∧ sleep_min<330 (>=3 ночи)."""
    hrv = _daily_series(cur, "hrv")
    if len(hrv) < 10:
        return None
    days = list(hrv.keys())[-10:]
    baseline_days, check_days = days[:-3], days[-3:]
    hrv_baseline = statistics.median(hrv[d] for d in baseline_days)

    rhr = _daily_series(cur, "rhr")
    sleep = _daily_series(cur, "sleep_min")
    rhr_baseline_days = [d for d in baseline_days if d in rhr]
    if not rhr_baseline_days:
        return None
    rhr_baseline = statistics.median(rhr[d] for d in rhr_baseline_days)

    hrv_hit = all(d in hrv and hrv[d] <= hrv_baseline * 0.7 for d in check_days)
    rhr_hit = all(d in rhr and rhr[d] >= rhr_baseline + 5 for d in check_days)
    sleep_hit = all(d in sleep and sleep[d] < 330 for d in check_days)

    if hrv_hit and rhr_hit and sleep_hit:
        return {
            "rule": "rf-c-02", "category": "systemic_warning", "level": "L1",
            "message": "признаки недовосстановления — стоит снизить нагрузку и выспаться",
            "trace": {"hrv_baseline": round(hrv_baseline, 1), "rhr_baseline": round(rhr_baseline, 1),
                      "check_days": [str(d) for d in check_days]},
        }
    return None


def run_layer_c(cur) -> list[dict]:
    results = []
    for check in (check_infectious_pattern, check_recovery_collapse):
        r = check(cur)
        if r:
            results.append(r)
    return results
