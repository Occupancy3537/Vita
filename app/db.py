"""Подключение к Postgres. Отдельная роль card_service (не n8n), пишет/читает схему
card (или card_test под pytest). Со Phase 0 нового доктора (2026-09-15) роль
дополнительно получила SELECT на 12 таблиц схемы health и INSERT/UPDATE на 4 из
них (symptom_log/doctor_notes/investigations/lab_plan) — осознанное ослабление
исходной изоляции (см. backups/infra/grant_health_to_card_service.sql,
NEW_DOCTOR_PLAN_2026-09-15.md §7.5): без этого доктор не может транзакционно
писать в те же таблицы, что читают дашборд/дозор/советник.

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
