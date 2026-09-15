"""Тестовое окружение: всегда card_test-схема, никогда не прод card. Секреты — из
.env.test (гитигнорится), см. .env.test.example."""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.test"))
os.environ["CARD_PG_SCHEMA"] = "card_test"  # жёстко, не полагаемся на .env.test

import pytest  # noqa: E402

from app.db import get_conn, schema  # noqa: E402


TABLES_TO_CLEAN = [
    "source_message", "extraction", "fact", "episode", "problem", "intervention",
    "opinion", "disagreement", "recommendation", "expectation", "recommendation_verdict",
    "visit", "lab_result", "memory_note", "journal", "entity_index", "metric_coverage",
    "rf_event", "rf_session", "dialog_turn", "agent_step",
]


@pytest.fixture(autouse=True)
def clean_all_tables():
    """Пустые таблицы перед каждым тестом — тесты не должны зависеть друг от друга."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(schema() + '.' + t for t in TABLES_TO_CLEAN)}")
        conn.commit()
    yield
