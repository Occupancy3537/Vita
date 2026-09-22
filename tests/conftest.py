"""Тестовое окружение: всегда card_test-схема, никогда не прод card. Секреты — из
.env.test (гитигнорится), см. .env.test.example."""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.test"))
os.environ["CARD_PG_SCHEMA"] = "card_test"  # жёстко, не полагаемся на .env.test
# Волна 3 (B2): дуал-райт регистратора пишет в health.visits/results — в тестах
# эти цели перенаправлены в card_test-копии той же формы (прод health.* тесты
# не касаются вообще).
os.environ["REGISTRAR_HEALTH_SCHEMA"] = "card_test"
# T3 (2026-09-23): кеш чтения зоны в timeutil — в тестах выключен, иначе
# смена зоны в одном тесте протекала бы в соседние (TTL 60 с).
os.environ["TIMEUTIL_TZ_CACHE_SECONDS"] = "0"

import pytest  # noqa: E402

from app.db import get_conn, schema  # noqa: E402

# Тестовые двойники health.visits/health.results (DDL = pg_schema_phaseC.sql).
with get_conn() as _conn, _conn.cursor() as _cur:
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.visits (
          "Visit_ID" text PRIMARY KEY, "Date" text, "Age_at_Visit" text,
          "Lab_Name" text, "Notes" text, _synced_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.results (
          "Visit_ID" text, "Marker_ID" text, "Value" text, "Original_Unit" text,
          "Lab_Min" text, "Lab_Max" text, _synced_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY ("Visit_ID", "Marker_ID"))
    """)
    # 2026-09-21 (#38/#47, аудит ZCode): двойники для app/doctor/commit.py —
    # раньше писал буквально в health.symptom_log/doctor_notes/investigations/
    # lab_plan, из-за чего "известные 5 падений" test_doctor_commit.py на самом
    # деле были не багом, а конфликтом с настоящей открытой записью Влада в
    # health.investigations (одновременно только одно открытое расследование —
    # инвариант отрабатывал правильно, просто на проде, не на тестовых данных).
    # DDL упрощён относительно прод-схемы (без identity/trigger updated_at) —
    # ни один тест на эти детали не полагается, тот же принцип, что у visits/results.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.symptom_log (
          id bigserial PRIMARY KEY, symptom_id text NOT NULL, ts timestamptz NOT NULL,
          symptom text, system text, severity text, status text, change text,
          domain text, context text, hypothesis text, notes text,
          source text NOT NULL DEFAULT 'AI-доктор', created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.doctor_notes (
          id bigserial PRIMARY KEY, note_date date, category text, note text,
          trigger text, plan text, doctor text, source text NOT NULL DEFAULT 'AI-доктор',
          created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.investigations (
          inv_id text PRIMARY KEY, opened date, trigger text, trigger_detail text,
          hypothesis text, status text, findings text, questions_pending text,
          labs_suggested text, doctor_brief text, referral text, updated date, closed date,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.lab_plan (
          "Plan_ID" text PRIMARY KEY, "Test" text, "Category" text, "Interval_Months" text,
          "Last_Done" text, "Next_Due" text, "Reason" text, "Status" text, "Source" text,
          "Notes" text, _synced_at timestamptz NOT NULL DEFAULT now())
    """)
    # Фаза 0 плана TIME_AND_MULTIUSER_PLAN_2026-09-22: app/timeutil.py читает
    # зону человека из {HEALTH_SCHEMA}.people — в тестах это card_test (тот же
    # переключатель REGISTRAR_HEALTH_SCHEMA, что у commit.py/registrar.py).
    # DDL = pg_schema_people.sql (прод) + строка self: тесты идут по реальному
    # пути чтения зоны, а не только по fail-safe фолбэку.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.people (
          id text PRIMARY KEY, name text NOT NULL, birth_year int,
          home_tz text NOT NULL DEFAULT 'Asia/Vladivostok',
          current_tz text NOT NULL DEFAULT 'Asia/Vladivostok',
          locale text NOT NULL DEFAULT 'ru',
          created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(
        f"INSERT INTO {schema()}.people (id, name, birth_year) VALUES ('self', 'тест', 1982) "
        "ON CONFLICT (id) DO NOTHING"
    )
    # Страница «Настройки» (2026-09-22): журнал прогонов циклов + метрики хоста
    # (DDL = pg_schema_status_page.sql). DDL в card_test — чтобы юниты не писали
    # в боевые card.*/health.* (тест backup_alert уже показал цену такой ошибки).
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.scheduler_run_log (
          name text PRIMARY KEY, last_ok_at timestamptz,
          last_error text, last_error_at timestamptz)
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.host_metrics (
          ts timestamptz PRIMARY KEY, load1 real, load5 real, load15 real,
          mem_used_mb int, swap_used_mb int)
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.llm_usage (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(), module text NOT NULL, model text,
          tokens_prompt int, tokens_completion int, cost_usd numeric(12, 6))
    """)
    _conn.commit()


TABLES_TO_CLEAN = [
    "source_message", "extraction", "fact", "episode", "problem", "intervention",
    "opinion", "disagreement", "recommendation", "expectation", "recommendation_verdict",
    "visit", "lab_result", "memory_note", "journal", "entity_index", "metric_coverage",
    "rf_event", "rf_session", "dialog_turn", "agent_step",
    "visits", "results",  # двойники health.* для дуал-райта регистратора
    "symptom_log", "doctor_notes", "investigations", "lab_plan",  # двойники health.* для commit.py
    "scheduler_run_log", "host_metrics",  # статус-страница (run_log/host_metrics)
    "llm_usage",  # учёт стоимости LLM вне доктора
]


@pytest.fixture(autouse=True)
def clean_all_tables():
    """Пустые таблицы перед каждым тестом — тесты не должны зависеть друг от друга."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(schema() + '.' + t for t in TABLES_TO_CLEAN)}")
        conn.commit()
    yield
