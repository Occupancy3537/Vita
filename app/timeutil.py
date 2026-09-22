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
import time
from datetime import date, datetime, timedelta, timezone
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


# --- расписания в зоне человека (Фаза 3 плана TIME_AND_MULTIUSER) -----------

def next_occurrence(hour: int, minute: int = 0, *, weekday: Optional[int] = None,
                    day_of_month: Optional[int] = None,
                    person_id: str = SELF_PERSON_ID,
                    now: Optional[datetime] = None,
                    tz: Optional[ZoneInfo] = None) -> datetime:
    """Ближайший момент hour:minute в зоне человека (aware). Чистая функция —
    вся арифметика расписаний живёт здесь (тестируется без сна):
    - без weekday/day_of_month — ежедневно;
    - weekday=6 — ближайшее воскресенье (0=Пн, как в Python);
    - day_of_month=1 — ближайшее 1-е число месяца.
    """
    tz = tz or person_tz(person_id)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if day_of_month is not None:
        if not (now.day == day_of_month and now < nxt):
            year, month = now.year, now.month + 1
            if month > 12:
                year, month = year + 1, 1
            nxt = nxt.replace(year=year, month=month, day=day_of_month)
        return nxt

    if weekday is not None:
        nxt = nxt + timedelta(days=(weekday - nxt.weekday()) % 7)
        if nxt <= now:
            nxt = nxt + timedelta(days=7)
        return nxt

    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return nxt


def sleep_until_local(hour: int, minute: int = 0, *, weekday: Optional[int] = None,
                      day_of_month: Optional[int] = None,
                      person_id: str = SELF_PERSON_ID,
                      chunk_seconds: float = 600.0) -> None:
    """Спать до ближайшего hour:minute ПО ЧАСАМ ЧЕЛОВЕКА (Фаза 3: расписания
    следуют за путешественником). Спит кусками по chunk_seconds и пересчитывает
    цель — смена зоны (/tz) подхватывается в пределах куска, а не «со
    следующего срабатывания». Машинные расписания (бэкап, docker prune) сюда
    не переводятся — они про сервер, не про суточный ритм человека."""
    while True:
        nxt = next_occurrence(hour, minute, weekday=weekday, day_of_month=day_of_month,
                              person_id=person_id)
        remaining = (nxt - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, chunk_seconds))
        if remaining <= chunk_seconds:
            return
