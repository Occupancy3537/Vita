"""
Движок вердиктов (П3 §4) — чистая функция от (facts, expectation, engine_config).
Тестируется на фикстурах, воспроизводима по rule_trace. Единственный тип ожидания,
полностью реализованный сейчас — delta_abs (совпадает с тем, что реально накопила
история action_loops: среднее до/после). threshold/frequency/subjective — честно
data_gap, не притворяются посчитанными (Rec9: "данных не хватило" ≠ "нет эффекта",
но и "тип не реализован" ≠ "эффекта нет" — тот же принцип честности).

Формулы — 1:1 из спецификации (rv-engine/1):
  effective : d·m ≥ 0.8·|e|
  partial   : 0.3·|e| ≤ d·m < 0.8·|e|
  no_effect : d·m < 0.3·|e| ∧ |m| ≤ 1.5σ
  adverse   : d·m < 0 ∧ |m| > 1.5σ
Плюс overshoot-оговорка (§4.2): верное направление, |m| > 3|e| -> caveat в rule_trace,
не отдельный вердикт.
"""
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

ENGINE_VERSION = "rv-engine/1"

Verdict = Literal["effective", "partial", "no_effect", "adverse", "data_gap"]

TOL_EFFECTIVE = 0.8
TOL_PARTIAL = 0.3
ADVERSE_SIGMA = 1.5
OVERSHOOT_SIGMA = 3.0
COVERAGE_MIN = 0.70


@dataclass
class Fact:
    ts_event: datetime
    value_num: float


@dataclass
class Expectation:
    metric_key: str
    type: str  # delta_abs | delta_rel | threshold | frequency | subjective
    direction: str  # up | down
    magnitude: float
    window_days: int
    lag_days: int = 0
    baseline_days: int = 7


@dataclass
class VerdictResult:
    verdict: Verdict
    baseline_value: Optional[float]
    eval_value: Optional[float]
    personal_sigma: Optional[float]
    coverage: dict
    rule_trace: dict
    engine_version: str = ENGINE_VERSION


def _window_facts(facts: list[Fact], frm: datetime, to: datetime) -> list[float]:
    return [f.value_num for f in facts if frm <= f.ts_event < to]


def evaluate(started_ts: datetime, ex: Expectation, all_facts: list[Fact]) -> VerdictResult:
    """all_facts: вся история этой metric_key, любой давности — окна вырезаются здесь."""
    baseline_from = started_ts - timedelta(days=ex.baseline_days)
    baseline_to = started_ts
    lag_to = started_ts + timedelta(days=ex.lag_days)
    eval_to = lag_to + timedelta(days=ex.window_days)

    if ex.type != "delta_abs":
        return VerdictResult(
            verdict="data_gap", baseline_value=None, eval_value=None, personal_sigma=None,
            coverage={"baseline": 0, "eval": 0},
            rule_trace={"reason": f"тип ожидания '{ex.type}' пока не реализован движком — не притворяемся, что посчитали"},
        )

    baseline_vals = _window_facts(all_facts, baseline_from, baseline_to)
    eval_vals = _window_facts(all_facts, lag_to, eval_to)

    baseline_cov = len(baseline_vals) / max(ex.baseline_days, 1)
    eval_cov = len(eval_vals) / max(ex.window_days, 1)
    coverage = {"baseline": round(min(baseline_cov, 1.0), 2), "eval": round(min(eval_cov, 1.0), 2)}

    if baseline_cov < COVERAGE_MIN or eval_cov < COVERAGE_MIN:
        return VerdictResult(
            verdict="data_gap",
            baseline_value=statistics.median(baseline_vals) if baseline_vals else None,
            eval_value=statistics.median(eval_vals) if eval_vals else None,
            personal_sigma=None, coverage=coverage,
            rule_trace={"reason": "покрытие ниже порога 70%", "baseline_n": len(baseline_vals), "eval_n": len(eval_vals)},
        )

    baseline_mean = statistics.median(baseline_vals)
    eval_mean = statistics.median(eval_vals)

    sigma_window_facts = _window_facts(all_facts, started_ts - timedelta(days=90), started_ts)
    personal_sigma = statistics.pstdev(sigma_window_facts) if len(sigma_window_facts) >= 2 else 0.0

    m = eval_mean - baseline_mean
    e = ex.magnitude
    d = 1 if ex.direction == "up" else -1
    dm = d * m

    trace = {"m": round(m, 4), "e": e, "d": d, "sigma": round(personal_sigma, 4), "dm": round(dm, 4)}

    if dm >= TOL_EFFECTIVE * abs(e):
        verdict = "effective"
        if abs(m) > OVERSHOOT_SIGMA * abs(e):
            trace["caveat"] = "overshoot: |m| > 3|e| — подозрительно хороший результат, стоит проверить данные"
    elif dm >= TOL_PARTIAL * abs(e):
        verdict = "partial"
    elif dm < 0 and abs(m) > ADVERSE_SIGMA * personal_sigma:
        verdict = "adverse"
    else:
        verdict = "no_effect"

    return VerdictResult(
        verdict=verdict, baseline_value=round(baseline_mean, 4), eval_value=round(eval_mean, 4),
        personal_sigma=round(personal_sigma, 4), coverage=coverage, rule_trace=trace,
    )
