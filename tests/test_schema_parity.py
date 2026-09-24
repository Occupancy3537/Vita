"""ROADMAP 0.2/0.7 (2026-09-24): card_test обязана быть 1:1 с боевой card —
объектная модель, а не свалка вспомогательных таблиц. До этого захода
card_test попутно хранила 7 двойников health.* (visits/results/symptom_log/
doctor_notes/investigations/lab_plan/people, нужны registrar.py/commit.py/
timeutil.py через REGISTRAR_HEALTH_SCHEMA) — из-за чего сравнение card
и card_test было в принципе невозможно (внешнее ревью нашло это как
"дрейф схем", хотя на деле это смешение двух разных ролей в одной схеме).
Двойники переехали в отдельную health_test (tests/conftest.py), card_test
теперь содержит только объектную модель card.* — 1:1 с прод, и это можно
и нужно проверять автоматически, а не наделяться раз в квартал вручную.

Сравниваются ИМЕНА таблиц и (таблица, колонка, тип данных) — не project'ит
на NULL/DEFAULT/индексы/constraints (этого достаточно, чтобы поймать
"добавили колонку туда и забыли сюда", не более и не менее)."""
from app.db import get_conn


def _table_names(cur, schema_name: str) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema_name,),
    )
    return {r[0] for r in cur.fetchall()}


def _columns(cur, schema_name: str) -> set[tuple[str, str, str]]:
    cur.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s",
        (schema_name,),
    )
    return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def test_card_test_has_the_same_tables_as_card():
    with get_conn() as conn, conn.cursor() as cur:
        card_tables = _table_names(cur, "card")
        test_tables = _table_names(cur, "card_test")
    missing_in_test = card_tables - test_tables
    extra_in_test = test_tables - card_tables
    assert not missing_in_test, f"есть в card, нет в card_test: {sorted(missing_in_test)}"
    assert not extra_in_test, f"есть в card_test, нет в card (мусор/двойник не той схемы?): {sorted(extra_in_test)}"


def test_card_test_has_the_same_columns_as_card():
    with get_conn() as conn, conn.cursor() as cur:
        card_cols = _columns(cur, "card")
        test_cols = _columns(cur, "card_test")
    missing_in_test = card_cols - test_cols
    extra_in_test = test_cols - card_cols
    assert not missing_in_test, f"есть в card, нет в card_test: {sorted(missing_in_test)}"
    assert not extra_in_test, f"есть в card_test, нет в card: {sorted(extra_in_test)}"
