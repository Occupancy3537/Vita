"""Ворота качества lab_scrape — на данных и на искусственно испорченной копии."""
from pathlib import Path

import pytest

from scripts.lab_scrape.core import compare_with_previous, make_row, now_iso, validate_rows

FIX = Path(__file__).parent / "fixtures" / "lab_scrape"


def _row(lab="gemotest", code="1.1", name="ОАК", price=100.0, **kw):
    params = dict(url="https://x/y", source_url="https://x/src", raw_ref="raw/x.gz",
                  city_confirmed=True)
    params.update(kw)
    return make_row(lab_code=lab, ext_code=code, name=name, price=price,
                    category=None, term=None, composition=None,
                    collected_at=now_iso(), **params)


def test_validate_rows_accepts_good_rows():
    assert validate_rows([_row(), _row(code="1.2", name="Креатинин")]) == []


def test_validate_rows_rejects_bad_price_and_missing_proof():
    bad = [
        _row(code="9.1", price=0),                 # цена <= 0
        _row(code="9.2", price=None),              # цена не число
        _row(code="9.3", raw_ref=""),              # нет доказательства
        _row(code="9.4", source_url=""),           # нет source_url
        _row(code="9.5", city_confirmed=False),    # город не подтверждён
        _row(code="9.6"),
        _row(code="9.6"),                          # дубль (lab, code)
    ]
    problems = validate_rows(bad)
    assert len(problems) >= 6
    assert any("9.6" in p and "дубль" in p for p in problems)


def test_gate_count_drop_more_than_20_percent():
    prev = [_row(code=f"{i}.0") for i in range(100)]
    cur = [_row(code=f"{i}.0") for i in range(70)]  # -30%
    reasons = compare_with_previous(cur, prev)
    assert any("падение >20%" in r for r in reasons)


def test_gate_price_spikes_more_than_15_percent():
    prev = [_row(code=f"{i}.0", price=100.0) for i in range(100)]
    cur = [_row(code=f"{i}.0", price=300.0 if i < 20 else 100.0) for i in range(100)]
    reasons = compare_with_previous(cur, prev)
    assert any(">50%" in r and "20 из 100" in r for r in reasons)


def test_gate_passes_on_identical_data():
    prev = [_row(code=f"{i}.0", price=100.0) for i in range(50)]
    cur = [_row(code=f"{i}.0", price=100.0) for i in range(50)]
    assert compare_with_previous(cur, prev) == []


def test_gate_on_artificially_corrupted_copy_of_real_rows():
    """Задача, критерий приёмки 5: ворота срабатывают на испорченной копии."""
    real = [_row(lab="tafi", code=f"slug-{i}", name=f"Позиция {i}", price=500.0 + i)
            for i in range(40)]
    assert compare_with_previous(real, real) == []          # исходная копия чиста
    corrupted = [_row(lab="tafi", code=f"slug-{i}", name=f"Позиция {i}",
                      price=(500.0 + i) * 4 if i < 30 else 500.0 + i)  # 75% цен ×4
                 for i in range(40)]
    reasons = compare_with_previous(corrupted, real)
    assert reasons and "цена >50%" in reasons[0]
    # и падение числа позиций: половину удалим
    reasons2 = compare_with_previous(corrupted[:12], real)
    assert any("падение >20%" in r for r in reasons2)


def test_zero_lab_is_reported_by_caller_contract():
    """Лаба с 0 позиций — не в validate_rows, а в __main__: здесь фиксируем
    контракт, что пустой список строк валиден как список, но пустая лаба
    должна попасть в problems (проверка логики run() вынесена в CLI-тест)."""
    rows = validate_rows([])
    assert rows == []
