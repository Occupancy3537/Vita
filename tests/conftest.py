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
    _conn.commit()


TABLES_TO_CLEAN = [
    "source_message", "extraction", "fact", "episode", "problem", "intervention",
    "opinion", "disagreement", "recommendation", "expectation", "recommendation_verdict",
    "visit", "lab_result", "memory_note", "journal", "entity_index", "metric_coverage",
    "rf_event", "rf_session", "dialog_turn", "agent_step",
    "visits", "results",  # двойники health.* для дуал-райта регистратора
]


@pytest.fixture(autouse=True)
def clean_all_tables():
    """Пустые таблицы перед каждым тестом — тесты не должны зависеть друг от друга."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(schema() + '.' + t for t in TABLES_TO_CLEAN)}")
        conn.commit()
    yield
