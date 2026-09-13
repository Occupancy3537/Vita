"""Подключение к Postgres. Отдельная роль card_service (не n8n) — только на схему
card (или card_test под pytest), доступа к схеме health нет физически (проверено
руками при создании роли, не только предполагается).

Пока без пула соединений — по объёму трафика (единицы сообщений в день, П5-спека
§10) не нужен; вернуться к psycopg_pool, если/когда реальная нагрузка это оправдает.
"""
import os

import psycopg


def get_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["CARD_PG_HOST"],
        port=os.environ.get("CARD_PG_PORT", "5432"),
        user=os.environ["CARD_PG_USER"],
        password=os.environ["CARD_PG_PASSWORD"],
        dbname=os.environ.get("CARD_PG_DATABASE", "health"),
    )


def schema() -> str:
    """Имя схемы: card в проде, card_test под pytest (см. tests/conftest.py)."""
    return os.environ.get("CARD_PG_SCHEMA", "card")
