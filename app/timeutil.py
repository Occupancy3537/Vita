"""Единый источник правила «какой день у человека» (Фаза 0 плана
TIME_AND_MULTIUSER_PLAN_2026-09-22; находки T1/T2/T3 внешнего аудита логики).

Принципы (план, раздел 1): момент события — timestamptz/UTC (не трогаем);
«день» события — в зоне ЧЕЛОВЕКА на момент события (IANA-строка из
health.people: при путешествии current_tz меняется, home_tz остаётся);
зона — свойство человека, не константа системы.

Объём Фазы 0: резолвер «чат → человек» появится в Фазе 1, до тех пор
person_id везде 'self'. Fail-safe осознанный: любая проблема чтения зоны
(нет строки, нет таблицы, БД недоступна, неизвестная IANA-строка) — НЕ
исключение, а DEFAULT_TZ + warning. «Не смог посчитать день → упал весь ход
доктора» недопустимо; CURRENT_DATE в SQL запрещён (зона сервера UTC — это и
был T1: утренние записи доктора получали вчерашнюю дату).
"""
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.db import get_conn

logger = logging.getLogger(__name__)

DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Vladivostok")
SELF_PERSON_ID = "self"
# Тот же переключатель схемы, что у app/doctor/commit.py и app/registrar.py:
# tests/conftest.py задаёт card_test — тесты не читают боевую health.people.
_HEALTH_SCHEMA = os.environ.get("REGISTRAR_HEALTH_SCHEMA", "health")


def _read_tz_name(person_id: str) -> Optional[str]:
    """Имя зоны из people; None при любой проблеме (включая отсутствие таблицы
    до применения миграции pg_schema_people.sql)."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT COALESCE(current_tz, home_tz) FROM {_HEALTH_SCHEMA}.people WHERE id = %s",
                (person_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        logger.warning("timeutil: не удалось прочитать зону для %r — беру %s",
                       person_id, DEFAULT_TZ, exc_info=True)
        return None


def person_tz(person_id: str = SELF_PERSON_ID) -> ZoneInfo:
    """Зона человека: current_tz из people; при любой проблеме — DEFAULT_TZ."""
    tz_name = _read_tz_name(person_id) or DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("timeutil: неизвестная зона %r для %r — беру %s",
                       tz_name, person_id, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def now_local(person_id: str = SELF_PERSON_ID) -> datetime:
    """Текущий момент в зоне человека (aware datetime)."""
    return datetime.now(timezone.utc).astimezone(person_tz(person_id))


def today(person_id: str = SELF_PERSON_ID) -> date:
    """Сегодняшняя дата в зоне человека — замена CURRENT_DATE (T1)."""
    return now_local(person_id).date()


def local_day(ts: datetime, person_id: str = SELF_PERSON_ID) -> date:
    """День события ts в зоне человека. Наивный ts трактуем как UTC (в БД все
    моменты — timestamptz; наивные приходят только из тестов/старых данных)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(person_tz(person_id)).date()
