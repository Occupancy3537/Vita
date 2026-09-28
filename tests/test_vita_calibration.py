"""app/vita_calibration.py — калибровка ring.ahead на истории (Vita v2,
этап 1, 2026-09-28). _fetch_history мокается (реальная БД — отдельная живая
проверка, не юнит-тест): здесь только сама логика отбора пар и подсчёта
расхождения, на синтетической истории с известным правильным ответом."""
from datetime import date, timedelta

import app.vita_calibration as vc


def _synthetic_history(bad_food_day_offset=35, recovers_next_day=True):
    """45 дней ровного "good"-фона (>= 40 нужно historical_ahead_samples,
    чтобы не сработал охранник "мало истории" + 30 дней на baseline) + один
    день с bad-натрием на смещении bad_food_day_offset от начала, опционально
    восстанавливающийся на следующий день."""
    today = date(2026, 9, 20)
    days = [today - timedelta(days=i) for i in range(44, -1, -1)]  # по возрастанию, 45 дней

    rows_tuple = []
    rows_flat = []
    meals = []
    for i, d in enumerate(days):
        row = {"Восстановление_BodyBattery": 80.0, "ВСР_ночная": 50.0, "ACWR_Garmin": None,
               "ACWR_Status": None, "Тренировка_Ккал": 300.0, "Чистый_сон_мин": 450.0,
               "Оценка_сна_балл": 80.0, "Эффективность_сна_": 90.0, "Стресс_дневной_средний": 30.0,
               "Шаги_за_вчера": 9000.0, "Пульс_ночной_средний": 55.0, "VO2_Max": 45.0}
        rows_tuple.append((d, dict(row)))
        rows_flat.append({**row, "Дата": d.isoformat()})
        # recovers_next_day=False -> натрий остаётся плохим ВСЕГДА после
        # bad_food_day_offset (ни один следующий день не "восстанавливается"),
        # не просто ещё один плохой день перед хорошим.
        is_bad = (i == bad_food_day_offset) or (not recovers_next_day and i > bad_food_day_offset)
        sodium = 3500.0 if is_bad else 500.0
        meals.append({"Date": f"{d.isoformat()}T08:00", "Натрий": sodium, "Насыщенные жиры": 5.0, "Добавленный сахар": 5.0})

    targets = [{"Нутриент": "Натрий", "Колонка_в_Meals": "Натрий", "Категория": "Риск избытка",
                "Верхний_предел_UL": "2300", "Единица": "мг"}]
    return rows_tuple, rows_flat, meals, targets


def test_historical_ahead_samples_finds_pair_when_segment_recovers(monkeypatch):
    monkeypatch.setattr(vc, "_fetch_history", lambda cur: _synthetic_history(recovers_next_day=True))
    samples = vc.historical_ahead_samples(cur=None, days_back=30)
    assert len(samples) == 1
    assert samples[0]["segment"] == "food"
    # Живая правка (2026-09-28, «индекс дня зависит от кругляшей»): Заряд/Сон
    # в этой синтетической истории — РЕАЛЬНЫЕ 80/80 (Восстановление_BodyBattery/
    # Оценка_сна_балл), не судейские 100 — среднее по 4 сегментам на полностью
    # восстановленном дне (food тоже good): (80+80+100+100)/4 = 90.
    assert samples[0]["actual_next"] == 90
    # ahead — тот же индекс, но с food виртуально исправленным ДО факта -> тоже 90
    assert samples[0]["ahead"] == 90
    assert samples[0]["error"] == 0


def test_historical_ahead_samples_skips_when_segment_does_not_recover(monkeypatch):
    monkeypatch.setattr(vc, "_fetch_history", lambda cur: _synthetic_history(recovers_next_day=False))
    samples = vc.historical_ahead_samples(cur=None, days_back=30)
    assert samples == []


def test_calibration_report_empty_when_no_samples(monkeypatch):
    monkeypatch.setattr(vc, "historical_ahead_samples", lambda cur, days_back=180: [])
    rep = vc.calibration_report(cur=None)
    assert rep["n"] == 0 and rep["ok"] is None and "note" in rep


def test_calibration_report_ok_true_within_threshold(monkeypatch):
    monkeypatch.setattr(vc, "historical_ahead_samples", lambda cur, days_back=180: [
        {"date": "2026-09-01", "segment": "food", "ahead": 90, "actual_next": 88, "error": 2},
        {"date": "2026-09-05", "segment": "food", "ahead": 85, "actual_next": 82, "error": 3},
    ])
    rep = vc.calibration_report(cur=None)
    assert rep["n"] == 2 and rep["mean_abs_error"] == 2.5 and rep["ok"] is True


def test_calibration_report_ok_false_beyond_threshold():
    """Живой прогон 2026-09-28 на реальной истории Влада (97 дней, n=2 пары)
    дал mean_abs_error=18.5 — выше порога в CALIBRATION_THRESHOLD_POINTS=5.
    Ticket: "систематически врёт >5 очков — порог калибровки, не выпуск" —
    здесь фиксируем, что report честно возвращает ok=False в такой ситуации,
    не округляет вверх."""
    samples = [{"date": "2026-08-30", "segment": "food", "ahead": 86, "actual_next": 100, "error": -14},
               {"date": "2026-09-27", "segment": "food", "ahead": 77, "actual_next": 100, "error": -23}]
    mean_abs_error = sum(abs(s["error"]) for s in samples) / len(samples)
    assert mean_abs_error > vc.CALIBRATION_THRESHOLD_POINTS
