"""app/biohacking_ingest.py — порт n8n Collect_Biohacking_Data (2026-09-20,
группа 3). Юниты на чистые хелперы + build_daily_trends_row сценарии +
build_upsert_sql (null-clobber защита) + process_ingest оркестрация
(все внешние вызовы замокан)."""
import math

import pytest

from app import biohacking_ingest as bi
from app.db import get_conn


# --- чистые хелперы -----------------------------------------------------------

def test_js_round1_rounds_half_up_not_banker():
    assert bi._js_round1(24.35) == 24.4  # Python round() даёт 24.3 (banker's) — не то, что нужно
    assert bi._js_round1(24.34) == 24.3
    assert bi._js_round1(None) is None


def test_rnd_handles_none_and_plain_numbers():
    # _rnd делает float() напрямую (как оригинальный toNumber внутри rnd()
    # в Code in JavaScript — там тоже Number(val), не comma-aware) —
    # запятая как разделитель приходит только из Sheets-строк, которые
    # проходят через отдельные хелперы (_get_multi/_to_number), не через _rnd.
    assert bi._rnd("24,3") is None
    assert bi._rnd(24.35) == 24.4
    assert bi._rnd(None) is None
    assert bi._rnd("") is None
    assert bi._rnd(float("nan")) is None


def test_parse_any_date_ms_handles_dot_and_iso_formats():
    ms_dot = bi._parse_any_date_ms("19.09.2026 08:30:00")
    ms_iso = bi._parse_any_date_ms("2026-09-19T08:30:00")
    ms_space = bi._parse_any_date_ms("2026-09-19 08:30:00")
    assert ms_dot == ms_iso == ms_space
    assert bi._parse_any_date_ms(None) is None
    assert bi._parse_any_date_ms("") is None


def test_parse_any_date_ms_handles_slash_format_as_mm_dd_like_original():
    # Оригинальный JS-парсер трактует "N/M/YYYY" как MM/DD/YYYY (d[0]->месяц,
    # d[1]->день) — сохранено 1:1, включая то, что "19/09/2026" (день>12
    # как "месяц") невалиден и там, и здесь.
    ms = bi._parse_any_date_ms("09/05/2026 08:30:00")  # месяц=09, день=05
    ms_dot_equiv = bi._parse_any_date_ms("05.09.2026 08:30:00")  # день=05, месяц=09
    assert ms == ms_dot_equiv
    assert bi._parse_any_date_ms("19/09/2026 08:30:00") is None  # 19 как месяц — невалидно, как в оригинале


def test_dew_point_magnus_formula():
    dp = bi._dew_point(20, 60)
    assert 11.5 < dp < 12.5  # для 20°C/60% точка росы ≈ 12.0°C


def test_dew_point_none_on_bad_input():
    assert bi._dew_point("не число", 60) is None
    assert bi._dew_point(20, None) is None


def test_dew_point_handles_comma_decimal_from_sheets():
    # НАЙДЕНО живой проверкой 2026-09-20: MicroClimate реально хранит
    # температуру с запятой ("26,4") — без coerce упало бы молча.
    dp_comma = bi._dew_point("20,0", "60")
    dp_dot = bi._dew_point("20.0", "60")
    assert dp_comma == dp_dot is not None


def test_js_str_drops_trailing_zero_like_js_string():
    assert bi._js_str(8000.0) == "8000"
    assert bi._js_str(8000) == "8000"
    assert bi._js_str(3.5) == "3.5"
    assert bi._js_str("текст") == "текст"


# --- build_upsert_sql -------------------------------------------------------

def test_build_upsert_sql_includes_only_present_known_keys():
    row = {"Дата": "2026-09-20", "Шаги_за_вчера": 12000, "НеизвестноеПоле": "x"}
    query, params = bi.build_upsert_sql(row)
    assert '"Шаги_за_вчера"' in query
    assert "НеизвестноеПоле" not in query
    assert params[0] == "2026-09-20"
    assert "12000" in params  # всё, кроме "Дата", уходит строкой — колонки TEXT


def test_build_upsert_sql_always_has_date_even_if_absent_from_keys_list():
    row = {"Дата": "2026-09-20"}
    query, params = bi.build_upsert_sql(row)
    assert '"Дата"' in query
    assert "ON CONFLICT" in query


def test_build_upsert_sql_no_set_clause_crash_when_only_date():
    # только "Дата" в строке — SET должен остаться валидным (хотя бы _synced_at)
    row = {"Дата": "2026-09-20"}
    query, _ = bi.build_upsert_sql(row)
    assert "SET _synced_at=now()" in query


def test_build_upsert_sql_escapes_percent_in_column_name():
    """Реальный инцидент 2026-09-21: "Влажность_avg_%" — единственная колонка
    в KNOWN_COLS с буквальным % в имени — ломала psycopg (client-side %s-
    биндинг сканирует ВЕСЬ текст запроса на %s/%b/%t, включая внутри кавычек:
    'only %s, %b, %t are allowed, got %"'). Заблокировала весь ночной сбор
    Garmin на день, пока не нашли. Запрос должен содержать %% (экранированный
    %), не голый %."""
    row = {"Дата": "2026-09-21", "Влажность_avg_%": 55}
    query, params = bi.build_upsert_sql(row)
    assert '"Влажность_avg_%%"' in query  # экранировано для psycopg
    assert '"Влажность_avg_%"' not in query  # не голый % — именно он и падал
    assert "55" in params


def test_build_upsert_sql_percent_column_actually_executes_in_postgres():
    """То же самое, но не текстовая проверка — реальный psycopg.execute()
    против настоящей health.daily_trends (query жёстко на неё ссылается,
    схема не параметризована). Юнит-тест выше ловит форму строки, но не сам
    факт, что psycopg согласится её выполнить — а именно это упало в проде.
    psycopg-соединение не в autocommit (умолчание psycopg3) — conn.rollback()
    откатывает INSERT до конца теста, боевая строка "Дата"=2026-09-21 не
    остаётся."""
    row = {"Дата": "2026-09-21", "Влажность_avg_%": "55"}
    query, params = bi.build_upsert_sql(row)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)  # не должно бросить psycopg.ProgrammingError
        conn.rollback()


# --- build_daily_trends_row --------------------------------------------------

def _base_garmin(**overrides) -> dict:
    g = {
        "date": "2026-09-20", "bedtime": "2026-09-19 23:30:00", "wakeup_time": "2026-09-20 07:30:00",
        "sleep_total_min": 420, "sleep_deep_min": 90, "sleep_rem_min": 80, "awake_count": 2,
        "awake_time_min": 10, "sleep_score": 85, "hr_night_avg": 52, "hrv_night": 55,
        "steps": 9000, "vo2max": 44, "workouts_raw": None, "late_workout_flag": False,
        "breathwork_type": None, "breathwork_min": 0,
    }
    g.update(overrides)
    return g


# --- longest_sedentary_gap_minutes / "Провал_без_движения_мин" / "Плавание_было" ---
# 2026-09-23, по запросу Влада: "основной инструмент — ходьба каждые 30 мин",
# после разбора грыжи L5/S1 по нескольким специальностям.

def test_longest_sedentary_gap_finds_the_worst_gap():
    # окно 07:00-23:00 = минуты 420-1380. Провал 480->600 (120 мин) — худший,
    # провал 700->750 (50 мин) — короче, не он.
    pts = [[420, 1], [480, 0], [540, 0], [600, 1], [700, 0.2], [750, 1]]
    assert bi.longest_sedentary_gap_minutes(pts) == 120


def test_longest_sedentary_gap_ignores_movement_outside_the_day_window():
    # провал в 3 часа ночи (минута 180) — вне окна 07:00-23:00, не считается
    pts = [[180, 0], [181, 0], [420, 1], [421, 1]]
    assert bi.longest_sedentary_gap_minutes(pts) == 0


def test_longest_sedentary_gap_open_ended_gap_counts_to_last_point():
    pts = [[420, 1], [500, 0], [560, 0]]  # провал с минуты 500 до конца данных (560)
    assert bi.longest_sedentary_gap_minutes(pts) == 60


def test_longest_sedentary_gap_none_when_no_data():
    assert bi.longest_sedentary_gap_minutes(None) is None
    assert bi.longest_sedentary_gap_minutes([]) is None


def test_build_row_includes_movement_gap_and_swim_flag():
    row = bi.build_daily_trends_row(
        _base_garmin(swam_yesterday=True, movement_minutes=[[420, 1], [480, 0], [560, 0], [600, 1]]),
        [], [], [], [], {}, {},
    )
    assert row["Провал_без_движения_мин"] == 120
    assert row["Плавание_было"] == "Да"


def test_build_row_no_swim_and_no_movement_data():
    row = bi.build_daily_trends_row(_base_garmin(), [], [], [], [], {}, {})
    assert row["Плавание_было"] == "Нет"
    assert "Провал_без_движения_мин" not in row  # None -> отфильтрован, как остальные пустые поля


def test_build_row_raises_on_empty_date():
    with pytest.raises(ValueError):
        bi.build_daily_trends_row(_base_garmin(date=""), [], [], [], [], {}, {})


def test_build_row_strips_empty_and_none_fields():
    row = bi.build_daily_trends_row(_base_garmin(), [], [], [], [], {}, {})
    assert "Дата" in row
    assert all(v is not None and v != "" for v in row.values())


def test_build_row_computes_sleep_efficiency():
    row = bi.build_daily_trends_row(_base_garmin(sleep_total_min=400, awake_time_min=20), [], [], [], [], {}, {})
    # in_bed=420, net_sleep=400 -> 95.2%
    assert row["Эффективность_сна_"] == "95.2%"


def test_build_row_dinner_after_17h_before_bedtime():
    nutrition = [
        {"Date": "2026-09-19T08:00", "Calories": "300", "Proteins": "10", "Fats": "5", "Carbs": "30"},
        {"Date": "2026-09-19T19:00", "Calories": "600", "Proteins": "40", "Fats": "20", "Carbs": "50"},
    ]
    row = bi.build_daily_trends_row(_base_garmin(), nutrition, [], [], [], {}, {})
    assert row["Ужин_время"] == "2026-09-19T19:00"
    assert row["Ужин_Ккал"] == 600


def test_build_row_fasting_window_and_breakfast():
    nutrition = [
        {"Date": "2026-09-19T20:00", "Calories": "500", "Proteins": "30", "Fats": "10", "Carbs": "40"},
        {"Date": "2026-09-20T08:00", "Calories": "300", "Proteins": "20", "Fats": "5", "Carbs": "30"},
    ]
    row = bi.build_daily_trends_row(_base_garmin(), nutrition, [], [], [], {}, {})
    assert row["Завтрак_время"] == "2026-09-20T08:00"
    # ужин 20:00 -> отбой 23:30 = 3.5ч окна голода до сна
    assert row["Окно_голода_до_сна_ч"] == 3.5


def test_build_row_alcohol_and_coffee():
    nutrition = [
        {"Date": "2026-09-19T20:00", "Calories": "500", "Алкоголь": "15", "кофеин": "80"},
    ]
    row = bi.build_daily_trends_row(_base_garmin(), nutrition, [], [], [], {}, {})
    assert row["Алкоголь_гр"] == 15
    assert row["Время_последнего_кофе"] == "2026-09-19T20:00"


def test_build_row_climate_averaged_only_during_sleep_hours():
    climate = [
        {"Дата": "2026-09-19 12:00:00", "Температура": "30", "Влажность": "40"},  # день, не считается
        {"Дата": "2026-09-20 02:00:00", "Температура": "22", "Влажность": "50"},  # ночь, считается
        {"Дата": "2026-09-20 04:00:00", "Температура": "24", "Влажность": "50"},  # ночь, считается
    ]
    row = bi.build_daily_trends_row(_base_garmin(), [], climate, [], [], {}, {})
    assert row["Температура_avg_C"] == 23.0  # (22+24)/2, дневная запись исключена


def test_build_row_climate_handles_comma_decimal_temperature():
    # реальный формат MicroClimate (сверено живым запросом 2026-09-20)
    climate = [
        {"Дата": "2026-09-20 02:00:00", "Температура": "22,5", "Влажность": "50"},
        {"Дата": "2026-09-20 04:00:00", "Температура": "23,5", "Влажность": "50"},
    ]
    row = bi.build_daily_trends_row(_base_garmin(), [], climate, [], [], {}, {})
    assert row["Температура_avg_C"] == 23.0


def test_build_row_meds_from_calendar_titles():
    events = [
        {"summary": "Магний 400мг"},
        {"summary": "Встреча с коллегами"},
        {"summary": "Позвонить маме"},
        {"summary": "Витамин D3 капсула"},
    ]
    row = bi.build_daily_trends_row(_base_garmin(), [], [], events, [], {}, {})
    assert "Магний 400мг" in row["Лекарства_принимаемые"]
    assert "Витамин D3 капсула" in row["Лекарства_принимаемые"]
    assert "Встреча" not in row["Лекарства_принимаемые"]
    assert "Позвонить" not in row["Лекарства_принимаемые"]


def test_build_row_workouts_raw_parsing():
    row = bi.build_daily_trends_row(_base_garmin(workouts_raw="Бег|30|250;;Плавание|20|150"), [], [], [], [], {}, {})
    assert row["Тренировка_1_Тип"] == "Бег"
    assert row["Тренировка_1_Мин"] == 30
    assert row["Тренировка_2_Тип"] == "Плавание"
    assert row["Тренировка_Ккал"] == 400


def test_build_row_screen_time_from_rescuetime():
    rescue = [
        ["2026-09-19T10:00:00", 1800, None, 2],   # deep work, засчитается (вчера)
        ["2026-09-19T14:00:00", 900, None, -1],   # distraction
        ["2026-08-01T10:00:00", 500, None, 1],    # не вчера — не считается
    ]
    row = bi.build_daily_trends_row(_base_garmin(), [], [], [], rescue, {}, {})
    # 2700с = 0.75ч -> _js_round1 (round-half-up на 1 знак) даёт 0.8, не 0.75
    assert row["Экранное_время_всего_ч"] == 0.8
    assert row["Экран_Отвлечения_ч"] == 0.3  # 900с = 0.25ч -> тоже вверх до 0.3


def test_build_row_no_workout_defaults_to_net(monkeypatch=None):
    row = bi.build_daily_trends_row(_base_garmin(), [], [], [], [], {}, {})
    assert row["Тренировка_1_Тип"] == "Нет"
    assert row["Дыхание_тип"] == "Нет"


# --- compute_pressure_deltas -------------------------------------------------

def test_compute_pressure_deltas_empty_weather_returns_empty():
    assert bi.compute_pressure_deltas({}) == {}


def test_compute_pressure_deltas_missing_hourly_returns_empty():
    assert bi.compute_pressure_deltas({"hourly": {}}) == {}


# --- process_ingest (полностью замоканная оркестрация) -----------------------

TEST_DATE = "1999-12-31"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.daily_trends WHERE "Дата" = %s', (TEST_DATE,))
        cur.execute("DELETE FROM health.garmin_ingest_log WHERE date = %s", (TEST_DATE,))
        conn.commit()


def test_process_ingest_writes_row_and_calls_dependents(monkeypatch):
    monkeypatch.setattr(bi, "fetch_nutrition", lambda cur: [])
    monkeypatch.setattr(bi, "fetch_climate", lambda: [])
    monkeypatch.setattr(bi, "fetch_calendar_events", lambda d: [])
    monkeypatch.setattr(bi, "fetch_rescuetime", lambda d: [])
    monkeypatch.setattr(bi, "fetch_weather", lambda: {})
    monkeypatch.setattr(bi, "compute_pressure_deltas", lambda w: {})
    monkeypatch.setattr(bi, "sync_device_facts", lambda row: None)

    import app.anomaly_detector as ad
    called = []
    monkeypatch.setattr(ad, "run_daily_check", lambda: called.append(True))

    payload = bi.BiohackingPayload(date=TEST_DATE, steps=8000, sleep_total_min=400)
    row = bi.process_ingest(payload)

    assert row["Дата"] == TEST_DATE
    assert called == [True]

    with get_conn() as conn, conn.cursor() as cur:
        # health.daily_trends хранит всё, кроме "Дата", как TEXT — и без _js_str
        # тут было бы "8000.0" (pydantic коэрсит steps в float), см. _js_str
        cur.execute('SELECT "Шаги_за_вчера" FROM health.daily_trends WHERE "Дата" = %s', (TEST_DATE,))
        assert cur.fetchone() == ("8000",)


def test_process_ingest_survives_climate_and_calendar_failures(monkeypatch):
    monkeypatch.setattr(bi, "fetch_nutrition", lambda cur: [])

    def boom():
        raise ConnectionError("нет сети")
    monkeypatch.setattr(bi, "fetch_climate", lambda: (_ for _ in ()).throw(ConnectionError("нет сети")))
    monkeypatch.setattr(bi, "fetch_calendar_events", lambda d: (_ for _ in ()).throw(ConnectionError("нет сети")))
    monkeypatch.setattr(bi, "fetch_rescuetime", lambda d: [])
    monkeypatch.setattr(bi, "fetch_weather", lambda: (_ for _ in ()).throw(ConnectionError("нет сети")))
    monkeypatch.setattr(bi, "sync_device_facts", lambda row: None)

    import app.anomaly_detector as ad
    monkeypatch.setattr(ad, "run_daily_check", lambda: None)

    payload = bi.BiohackingPayload(date=TEST_DATE, steps=5000)
    row = bi.process_ingest(payload)  # не должно упасть, несмотря на отказ всех внешних API
    assert row["Дата"] == TEST_DATE


# --- премортем #7: сырой лог пишется первым, переигровка без дублей -----------

def _stub_external(monkeypatch):
    monkeypatch.setattr(bi, "fetch_nutrition", lambda cur: [])
    monkeypatch.setattr(bi, "fetch_climate", lambda: [])
    monkeypatch.setattr(bi, "fetch_calendar_events", lambda d: [])
    monkeypatch.setattr(bi, "fetch_rescuetime", lambda d: [])
    monkeypatch.setattr(bi, "fetch_weather", lambda: {})
    monkeypatch.setattr(bi, "compute_pressure_deltas", lambda w: {})
    monkeypatch.setattr(bi, "sync_device_facts", lambda row: None)
    import app.anomaly_detector as ad
    monkeypatch.setattr(ad, "run_daily_check", lambda: None)


def test_process_ingest_logs_raw_payload_before_processing(monkeypatch):
    _stub_external(monkeypatch)
    payload = bi.BiohackingPayload(date=TEST_DATE, steps=8000)
    bi.process_ingest(payload)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, raw_payload->>'steps' FROM health.garmin_ingest_log WHERE date = %s", (TEST_DATE,))
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "done"
    assert rows[0][1] == "8000.0"  # как отдал payload.model_dump() (float) — сырой лог не форматирует


def test_process_ingest_marks_log_failed_on_error_and_reraises(monkeypatch):
    monkeypatch.setattr(bi, "fetch_nutrition", lambda cur: [])
    monkeypatch.setattr(bi, "fetch_climate", lambda: [])
    monkeypatch.setattr(bi, "fetch_calendar_events", lambda d: [])
    monkeypatch.setattr(bi, "fetch_rescuetime", lambda d: [])

    def boom_weather():
        raise RuntimeError("сбой сборки строки")
    monkeypatch.setattr(bi, "fetch_weather", boom_weather)

    def boom_build(*a, **kw):
        raise RuntimeError("сбой сборки строки")
    monkeypatch.setattr(bi, "build_daily_trends_row", boom_build)

    payload = bi.BiohackingPayload(date=TEST_DATE, steps=1)
    with pytest.raises(RuntimeError):
        bi.process_ingest(payload)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error FROM health.garmin_ingest_log WHERE date = %s", (TEST_DATE,))
        row = cur.fetchone()
    assert row[0] == "failed"
    assert "сбой сборки строки" in row[1]


def test_reprocess_from_log_replays_without_duplicating_daily_trends_row(monkeypatch):
    """Регрессия премортема #7: переигровка того же дня — не второй ряд, а
    обновление того же самого (UPSERT по "Дата"), сама причина запрета
    "не запускай send_to_n8n.py вручную" здесь структурно снята."""
    _stub_external(monkeypatch)
    payload = bi.BiohackingPayload(date=TEST_DATE, steps=1000)
    bi.process_ingest(payload)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM health.garmin_ingest_log WHERE date = %s", (TEST_DATE,))
        log_id = cur.fetchone()[0]

    bi.reprocess_from_log(log_id)  # тот же payload, ещё раз

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*), "Шаги_за_вчера" FROM health.daily_trends WHERE "Дата" = %s GROUP BY "Шаги_за_вчера"', (TEST_DATE,))
        rows = cur.fetchall()
    assert len(rows) == 1  # одна строка, не две
    assert rows[0][0] == 1

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM health.garmin_ingest_log WHERE id = %s", (log_id,))
        assert cur.fetchone()[0] == "done (replay)"


def test_reprocess_from_log_raises_on_unknown_id():
    with pytest.raises(ValueError):
        bi.reprocess_from_log(999_999_999)
