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
    """threshold/frequency реализованы (2026-09-24, «петля исходов») — честный
    data_gap теперь проверяется на реально ещё не реализованном типе (subjective)."""
    ex = Expectation(metric_key="steps", type="subjective", direction="up", magnitude=10000,
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


# ═══════════════ threshold (2026-09-24, «петля исходов» часть 2) ═══════════════
# «агрегат метрики за окно против границы» — без baseline, см. docstring модуля.

def _threshold_ex(**kwargs):
    defaults = dict(metric_key="steps", type="threshold", direction="up", magnitude=12000.0, window_days=7, lag_days=0)
    defaults.update(kwargs)
    return Expectation(**defaults)


def test_threshold_effective_when_aggregate_meets_bound():
    ev = _facts([(i, 13000) for i in range(0, 7)])
    r = evaluate(STARTED, _threshold_ex(), ev)
    assert r.verdict == "effective"
    assert r.eval_value == 13000.0
    assert r.baseline_value is None  # threshold не сравнивает с "было"


def test_threshold_partial_within_30pct_slack_below_bound():
    # bound=12000, slack=0.3*12000=3600 -> gap>=-3600 -> partial. aggregate=10000: gap=-2000.
    ev = _facts([(i, 10000) for i in range(0, 7)])
    r = evaluate(STARTED, _threshold_ex(), ev)
    assert r.verdict == "partial"


def test_threshold_no_effect_beyond_slack():
    ev = _facts([(i, 6000) for i in range(0, 7)])  # gap = 6000-12000 = -6000 < -3600
    r = evaluate(STARTED, _threshold_ex(), ev)
    assert r.verdict == "no_effect"


def test_threshold_direction_down_bound_is_ceiling():
    # «жиры до 28г/день» — direction=down, magnitude=28: держаться НИЖЕ границы = effective.
    ex = _threshold_ex(metric_key="nutrition_total_fat_g", direction="down", magnitude=28.0)
    ev = _facts([(i, 25.0) for i in range(0, 7)])
    r = evaluate(STARTED, ex, ev)
    assert r.verdict == "effective"


def test_threshold_data_gap_below_coverage():
    ev = _facts([(i, 13000) for i in [0, 1, 2]])  # 3/7 < 70%
    r = evaluate(STARTED, _threshold_ex(), ev)
    assert r.verdict == "data_gap"


# ═══════════════ frequency (2026-09-24, «петля исходов» часть 2+3) ═══════════════
# Тикет: «вставать каждые 40 минут» = movement_gap_min <= 40 в >=5 из 7 дней;
# «плавание раз в неделю» = swam >= 1 в >=1 из 7 дней.

def _freq_ex(**kwargs):
    defaults = dict(metric_key="movement_gap_min", type="frequency", direction="down",
                     magnitude=40.0, window_days=7, lag_days=0, freq_min_ratio=5 / 7)
    defaults.update(kwargs)
    return Expectation(**defaults)


def test_frequency_effective_when_ratio_meets_min():
    # 5 из 7 дней <=40 -> achieved_ratio=5/7, min_ratio=5/7 -> effective
    ev = _facts([(0, 30), (1, 35), (2, 40), (3, 50), (4, 20), (5, 60), (6, 25)])
    r = evaluate(STARTED, _freq_ex(), ev)
    assert r.verdict == "effective"
    assert r.adherence_pct == round(5 / 7 * 100, 1)


def test_frequency_not_adhered_when_ratio_below_min_distinct_from_no_effect():
    # 2 из 7 дней <=40 -> ниже 5/7 -> not_adhered, НЕ no_effect (часть 3: разные вердикты).
    ev = _facts([(0, 30), (1, 35), (2, 90), (3, 80), (4, 70), (5, 60), (6, 100)])
    r = evaluate(STARTED, _freq_ex(), ev)
    assert r.verdict == "not_adhered"
    assert r.verdict != "no_effect"


def test_frequency_swim_example_from_ticket_low_bar_still_effective():
    # «плавание раз в неделю»: swam(1=Да) >= 1, freq_min_ratio=1/7 — один день довольно.
    ex = _freq_ex(metric_key="swam", direction="up", magnitude=1.0, freq_min_ratio=1 / 7)
    ev = _facts([(0, 0), (1, 0), (2, 1), (3, 0), (4, 0), (5, 0), (6, 0)])
    r = evaluate(STARTED, ex, ev)
    assert r.verdict == "effective"


def test_frequency_data_gap_until_window_fills():
    """Акс. критерий тикета буквально: движок частотный считает, но пока окно не
    заполнилось (< 70% дней с данными) — честный data_gap, не преждевременный
    not_adhered."""
    ev = _facts([(0, 30), (1, 35)])  # 2/7 = 29% < 70%
    r = evaluate(STARTED, _freq_ex(), ev)
    assert r.verdict == "data_gap"


# ═══════════════ not_adhered ≠ no_effect (часть 3) ═══════════════

def test_not_adhered_is_a_distinct_verdict_from_no_effect_by_type():
    """delta_abs никогда не производит not_adhered (у него нет понятия
    "частота выполнения") — not_adhered производит только frequency."""
    history_90d = _facts([(-i, 38 + (i % 5)) for i in range(8, 91)])
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 39) for i in range(0, 7)])
    r_delta = evaluate(STARTED, _full_coverage_ex(), history_90d + baseline + ev)
    assert r_delta.verdict == "no_effect"

    freq_ev = _facts([(0, 90), (1, 90), (2, 90), (3, 90), (4, 90), (5, 90), (6, 90)])
    r_freq = evaluate(STARTED, _freq_ex(), freq_ev)
    assert r_freq.verdict == "not_adhered"
    assert r_freq.verdict != r_delta.verdict


# ═══════════════ confounders (часть 5) ═══════════════

def test_confounders_annotate_without_changing_verdict():
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 46) for i in range(0, 7)])
    r_plain = evaluate(STARTED, _full_coverage_ex(), baseline + ev)
    r_confounded = evaluate(STARTED, _full_coverage_ex(), baseline + ev, confounders=["Витамин D3 + K2"])
    assert r_confounded.verdict == r_plain.verdict  # не меняет вердикт
    assert r_confounded.confounded == ["Витамин D3 + K2"]
    assert "конфаундер" in r_confounded.rule_trace["confounder_note"]


def test_no_confounders_leaves_confounded_empty():
    baseline = _facts([(-i, 40) for i in range(1, 8)])
    ev = _facts([(i, 46) for i in range(0, 7)])
    r = evaluate(STARTED, _full_coverage_ex(), baseline + ev, confounders=[])
    assert r.confounded == []
    assert "confounder_note" not in r.rule_trace
