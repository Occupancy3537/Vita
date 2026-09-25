"""
Движок вердиктов (П3 §4) — чистая функция от (facts, expectation, engine_config).
Тестируется на фикстурах, воспроизводима по rule_trace.

Три реализованных типа ожидания (2026-09-24, «петля исходов»):

  delta_abs — среднее ДО/ПОСЛЕ (единственный тип, реализованный до этого тикета):
    effective : d·m ≥ 0.8·|e|
    partial   : 0.3·|e| ≤ d·m < 0.8·|e|
    no_effect : d·m < 0.3·|e| ∧ |m| ≤ 1.5σ
    adverse   : d·m < 0 ∧ |m| > 1.5σ
    (m = eval_median - baseline_median, e = magnitude, d = +1 если direction='up' иначе -1)
    Плюс overshoot-оговорка (§4.2): верное направление, |m| > 3|e| -> caveat в
    rule_trace, не отдельный вердикт.

  threshold — агрегат (медиана) метрики за eval-окно ПРОТИВ границы magnitude,
    БЕЗ baseline (сравнивать не с "было", а с "нормой"/целью). slack = 0.3·|magnitude|
    (тот же TOL_PARTIAL, что и у delta_abs — единая политика допуска):
      gap = d·(aggregate - magnitude)   (d по тому же правилу, что у delta_abs)
      effective : gap ≥ 0                (граница выполнена или превышена с запасом)
      partial   : -slack ≤ gap < 0       (близко, не дотянули)
      no_effect : gap < -slack
    "adverse" для threshold честно не определён — без baseline нет референса,
    относительно которого "стало хуже" (в отличие от delta_abs, где baseline есть).

  frequency — доля дней eval-окна, в которые метрика УДОВЛЕТВОРЯЕТ дневному
    условию (direction+magnitude, то же правило, что у threshold, но применяется
    к КАЖДОМУ дню отдельно, не к агрегату), против ex.freq_min_ratio (доля,
    которую нужно набрать — по умолчанию 0.7, если не задано явно):
      achieved_ratio = дни_выполнено / window_days   (пропущенные дни = не выполнено —
                                                        строже, чем считать только по
                                                        дням с данными, честнее насчёт
                                                        реальной приверженности)
      effective    : achieved_ratio ≥ ex.freq_min_ratio
      not_adhered  : achieved_ratio < ex.freq_min_ratio (и покрытие набрано)
    adherence_pct = achieved_ratio·100 записывается в recommendation_verdict.adherence_pct
    (колонка существовала с самого начала объектной модели, ждала этот код).
    Примеры из тикета: «плавание раз в неделю» = direction=up, magnitude=1,
    freq_min_ratio=1/7≈0.14; «вставать каждые 40 минут» = direction=down,
    magnitude=40, freq_min_ratio=5/7≈0.71.

  subjective — по-прежнему честно data_gap (нет метрики вообще, качественная
    рекомендация — G7 требует unmeasurable_reason вместо этого типа).

not_adhered — НЕ синоним no_effect (П3 «петля исходов», часть 3): no_effect
  значит "вмешательство было, эффекта нет"; not_adhered значит "не знаем, было
  ли вмешательство — судя по частоте, скорее нет". Разные клинические выводы:
  первое просит пересмотреть вмешательство, второе — сначала спросить, выполнялось
  ли оно вообще. Пока производится только типом frequency (единственный тип,
  где "выполнялось ли" измеримо по card.fact). Политика приверженности лекарств
  (план для будущего раунда, Google Calendar пишет ПЛАН, не ФАКТ приёма — см.
  app/recommendations.py::evaluate_recommendation) использует тот же порог и
  тот же verdict через ЭТОТ ЖЕ frequency-путь, как только появится факт приёма.

Confounders (часть 5) — этот модуль ОСТАЁТСЯ чистой функцией: список
пересекающихся вмешательств/рекомендаций считает вызывающий (recommendations.py,
у которого есть курсор), передаёт сюда готовым списком названий. Задача этого
модуля — только положить его в rule_trace/confounded и не спутать с самим
вердиктом (confounder не меняет verdict, только помечает вывод как слабый).
Сезонных baseline'ов сознательно нет (данные копятся с июня 2026, пересмотреть
не раньше 2027) — задокументировано, не забыто.
"""
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional

ENGINE_VERSION = "rv-engine/2"

Verdict = Literal["effective", "partial", "no_effect", "adverse", "not_adhered", "data_gap"]

TOL_EFFECTIVE = 0.8
TOL_PARTIAL = 0.3
ADVERSE_SIGMA = 1.5
OVERSHOOT_SIGMA = 3.0
COVERAGE_MIN = 0.70
DEFAULT_FREQ_MIN_RATIO = 0.70


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
    freq_min_ratio: Optional[float] = None  # только для type='frequency'


@dataclass
class VerdictResult:
    verdict: Verdict
    baseline_value: Optional[float]
    eval_value: Optional[float]
    personal_sigma: Optional[float]
    coverage: dict
    rule_trace: dict
    engine_version: str = ENGINE_VERSION
    adherence_pct: Optional[float] = None
    confounded: list = field(default_factory=list)


def _window_facts(facts: list[Fact], frm: datetime, to: datetime) -> list[Fact]:
    return [f for f in facts if frm <= f.ts_event < to]


def _values(facts: list[Fact]) -> list[float]:
    return [f.value_num for f in facts]


def _apply_confounders(result: VerdictResult, confounders: Optional[list[str]]) -> VerdictResult:
    """Не меняет verdict — только помечает вывод как слабый (часть 5). Пустой
    список confounders — обычный случай, ничего не добавляет."""
    if confounders:
        result.confounded = list(confounders)
        result.rule_trace = {**result.rule_trace, "confounder_note": f"слабый вывод, конфаундер: {', '.join(confounders)}"}
    return result


def _evaluate_delta_abs(started_ts: datetime, ex: Expectation, all_facts: list[Fact]) -> VerdictResult:
    baseline_from = started_ts - timedelta(days=ex.baseline_days)
    baseline_to = started_ts
    lag_to = started_ts + timedelta(days=ex.lag_days)
    eval_to = lag_to + timedelta(days=ex.window_days)

    baseline_vals = _values(_window_facts(all_facts, baseline_from, baseline_to))
    eval_vals = _values(_window_facts(all_facts, lag_to, eval_to))

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

    sigma_window_facts = _values(_window_facts(all_facts, started_ts - timedelta(days=90), started_ts))
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


def _evaluate_threshold(started_ts: datetime, ex: Expectation, all_facts: list[Fact]) -> VerdictResult:
    lag_to = started_ts + timedelta(days=ex.lag_days)
    eval_to = lag_to + timedelta(days=ex.window_days)
    eval_vals = _values(_window_facts(all_facts, lag_to, eval_to))
    eval_cov = len(eval_vals) / max(ex.window_days, 1)
    coverage = {"baseline": None, "eval": round(min(eval_cov, 1.0), 2)}

    if eval_cov < COVERAGE_MIN:
        return VerdictResult(
            verdict="data_gap", baseline_value=None,
            eval_value=statistics.median(eval_vals) if eval_vals else None,
            personal_sigma=None, coverage=coverage,
            rule_trace={"reason": "покрытие ниже порога 70%", "eval_n": len(eval_vals)},
        )

    aggregate = statistics.median(eval_vals)
    d = 1 if ex.direction == "up" else -1
    gap = d * (aggregate - ex.magnitude)
    slack = TOL_PARTIAL * abs(ex.magnitude)

    trace = {"aggregate": round(aggregate, 4), "bound": ex.magnitude, "d": d, "gap": round(gap, 4), "slack": round(slack, 4)}

    if gap >= 0:
        verdict = "effective"
    elif gap >= -slack:
        verdict = "partial"
    else:
        verdict = "no_effect"

    return VerdictResult(
        verdict=verdict, baseline_value=None, eval_value=round(aggregate, 4),
        personal_sigma=None, coverage=coverage, rule_trace=trace,
    )


def _evaluate_frequency(started_ts: datetime, ex: Expectation, all_facts: list[Fact]) -> VerdictResult:
    lag_to = started_ts + timedelta(days=ex.lag_days)
    eval_to = lag_to + timedelta(days=ex.window_days)
    eval_facts = _window_facts(all_facts, lag_to, eval_to)
    # Один факт в день (device-факты — суточные агрегаты, см. biohacking_ingest.py) —
    # дедуп по календарному дню на случай повторной синхронизации того же дня.
    by_day = {f.ts_event.date(): f.value_num for f in eval_facts}
    days_n = len(by_day)
    eval_cov = days_n / max(ex.window_days, 1)
    coverage = {"baseline": None, "eval": round(min(eval_cov, 1.0), 2)}

    if eval_cov < COVERAGE_MIN:
        return VerdictResult(
            verdict="data_gap", baseline_value=None, eval_value=None,
            personal_sigma=None, coverage=coverage,
            rule_trace={"reason": "покрытие ниже порога 70% — окно ещё не заполнилось", "eval_n": days_n},
        )

    d = 1 if ex.direction == "up" else -1
    achieved_days = sum(1 for v in by_day.values() if d * (v - ex.magnitude) >= 0)
    achieved_ratio = achieved_days / max(ex.window_days, 1)
    min_ratio = ex.freq_min_ratio if ex.freq_min_ratio is not None else DEFAULT_FREQ_MIN_RATIO

    trace = {"achieved_days": achieved_days, "window_days": ex.window_days,
              "achieved_ratio": round(achieved_ratio, 4), "min_ratio": min_ratio}

    verdict = "effective" if achieved_ratio >= min_ratio else "not_adhered"

    return VerdictResult(
        verdict=verdict, baseline_value=None, eval_value=round(achieved_ratio, 4),
        personal_sigma=None, coverage=coverage, rule_trace=trace,
        adherence_pct=round(achieved_ratio * 100, 1),
    )


_EVALUATORS = {
    "delta_abs": _evaluate_delta_abs,
    "threshold": _evaluate_threshold,
    "frequency": _evaluate_frequency,
}


def evaluate(started_ts: datetime, ex: Expectation, all_facts: list[Fact],
             confounders: Optional[list[str]] = None) -> VerdictResult:
    """all_facts: вся история этой metric_key, любой давности — окна вырезаются здесь.
    confounders: названия пересекающихся активных вмешательств/рекомендаций — считает
    вызывающий (у него есть курсор), этот модуль остаётся чистой функцией (часть 5)."""
    evaluator = _EVALUATORS.get(ex.type)
    if evaluator is None:
        return VerdictResult(
            verdict="data_gap", baseline_value=None, eval_value=None, personal_sigma=None,
            coverage={"baseline": 0, "eval": 0},
            rule_trace={"reason": f"тип ожидания '{ex.type}' пока не реализован движком — не притворяемся, что посчитали"},
        )
    result = evaluator(started_ts, ex, all_facts)
    return _apply_confounders(result, confounders)
