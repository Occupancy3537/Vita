"""Golden-тесты движка вердиктов, включая граничные случаи (П3 §13: "ровно 0.8·|e|;
1.5σ; покрытие ровно 70%") — не только счастливый путь."""
from datetime import datetime, timedelta, timezone

from app.verdict_engine import Expectation, Fact, evaluate

STARTED = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _facts(day_offsets_and_values, base=STARTED):
    return [Fact(ts_event=base + timedelta(days=d), value_num=v) for d, v in day_offsets_and_values]


def _full_coverage_ex(**kwargs):
    defaults = dict(metric_key="hrv", type="delta_abs", direction="up", magnitude=6.0,
                     window_days=7, lag_days=0, baseline_days=7)
    defaults.update(kwargs)
    return Expectation(**defaults)


def test_effective_when_delta_meets_80pct_of_expected():
    # baseline: 7 дней по 40; eval: 7 дней по 46 (m=6, e=6, dm=6 >= 0.8*6=4.8) -> effective
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 46) for i in range(0, 7)])
    r = evaluate(STARTED, _full_coverage_ex(), baseline + ev)
    assert r.verdict == "effective"
    assert r.coverage == {"baseline": 1.0, "eval": 1.0}


def test_partial_between_30_and_80_pct():
    # m=3, e=6 -> dm=3, 0.3*6=1.8 <= 3 < 0.8*6=4.8 -> partial
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 43) for i in range(0, 7)])
    r = evaluate(STARTED, _full_coverage_ex(), baseline + ev)
    assert r.verdict == "partial"


def test_no_effect_when_below_30pct_and_within_sigma():
    # обычный для этого человека шум (sigma не крошечная, есть за 90 дней естественная
    # вариация 38-42) — небольшой сдвиг внутри этого шума не должен читаться как adverse.
    history_90d = _facts([(-i, 38 + (i % 5)) for i in range(8, 91)])
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 39) for i in range(0, 7)])  # m=-1, направление "up" не достигнуто, но это шум
    r = evaluate(STARTED, _full_coverage_ex(), history_90d + baseline + ev)
    assert r.verdict == "no_effect"


def test_adverse_when_wrong_direction_and_beyond_1_5_sigma():
    # direction=up (хотим рост), но метрика УПАЛА заметно сильнее шума истории.
    history_90d = _facts([(-i, 40) for i in range(8, 91)])  # низкий, стабильный шум -> малая sigma
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 20) for i in range(0, 7)])  # упало на 20 — точно не шум
    r = evaluate(STARTED, _full_coverage_ex(), history_90d + baseline + ev)
    assert r.verdict == "adverse"


def test_data_gap_when_coverage_below_70pct():
    # baseline: только 4 из 7 дней (57% < 70%) -> data_gap, не no_effect
    baseline = _facts([(-i, 40) for i in [1, 2, 3, 4]])
    ev = _facts([(i, 46) for i in range(0, 7)])
    r = evaluate(STARTED, _full_coverage_ex(), baseline + ev)
    assert r.verdict == "data_gap"
    assert r.coverage["baseline"] < 0.70


def test_data_gap_exactly_at_boundary_still_counts_as_covered():
    # ровно 70% (5 из 7 дней, доктрина округления) -> НЕ data_gap
    baseline = _facts([(-i, 40) for i in [1, 2, 3, 4, 5]])  # 5/7 = 0.714 >= 0.70
    ev = _facts([(i, 46) for i in range(0, 7)])
    r = evaluate(STARTED, _full_coverage_ex(), baseline + ev)
    assert r.verdict != "data_gap"


def test_unsupported_expectation_type_is_honest_data_gap_not_silent_wrong_answer():
    ex = Expectation(metric_key="steps", type="threshold", direction="up", magnitude=10000,
                      window_days=7, baseline_days=7)
    r = evaluate(STARTED, ex, _facts([(i, 12000) for i in range(-7, 7)]))
    assert r.verdict == "data_gap"
    assert "не реализован" in r.rule_trace["reason"]


def test_lag_days_excludes_early_window_from_eval():
    # lag=3: первые 3 дня после старта НЕ входят в eval-окно.
    ex = _full_coverage_ex(lag_days=3, window_days=7)
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    early_noise = _facts([(0, 999), (1, 999), (2, 999)])  # должны быть проигнорированы
    ev = _facts([(i, 46) for i in range(3, 10)])
    r = evaluate(STARTED, ex, baseline + early_noise + ev)
    assert r.eval_value == 46.0  # не искажено выбросами 999 из lag-окна


def test_rule_trace_is_reproducible_same_inputs_same_output():
    facts = _facts([(-i, 40) for i in range(1, 8)]) + _facts([(i, 46) for i in range(0, 7)])
    r1 = evaluate(STARTED, _full_coverage_ex(), facts)
    r2 = evaluate(STARTED, _full_coverage_ex(), facts)
    assert r1.verdict == r2.verdict
    assert r1.rule_trace == r2.rule_trace
