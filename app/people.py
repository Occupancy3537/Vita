"""Люди (Фаза 3 плана TIME_AND_MULTIUSER, 2026-09-22): сейчас — часовой пояс
человека (смена через команду боту /tz и страницу «Настройки»).

Фаза 1 плана (резолвер «чат → человек» через card.chat_person) отложена до
появления второго человека — здесь только то, что нужно уже сейчас: зона.
Принцип плана: зона — СВОЙСТВО ЧЕЛОВЕКА (IANA-строка), current_tz меняется в
путешествии, home_tz остаётся для «вернуться домой» и «не дома».
"""
import logging
import os
import zoneinfo
from typing import Optional
from zoneinfo import ZoneInfo

from app import timeutil
from app.db import get_conn

logger = logging.getLogger(__name__)

SELF_PERSON_ID = "self"
# Тот же переключатель схемы, что у timeutil/commit/registrar — тесты читают
# card_test, не боевую health.people.
_HEALTH_SCHEMA = os.environ.get("REGISTRAR_HEALTH_SCHEMA", "health")

# Примеры для подсказок (/tz и страница «Настройки») — частые зоны поездок;
# список открытый: принимается любое валидное IANA-имя.
TZ_EXAMPLES = [
    "Asia/Bangkok", "Asia/Dubai", "Europe/Istanbul", "Europe/Belgrade",
    "Asia/Shanghai", "Asia/Tokyo", "Europe/Berlin",
]


def get_person(person_id: str = SELF_PERSON_ID) -> Optional[dict]:
    """Строка человека (id, name, birth_year, home_tz, current_tz) или None."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, name, birth_year, home_tz, current_tz FROM {_HEALTH_SCHEMA}.people WHERE id = %s",
            (person_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "birth_year": row[2], "home_tz": row[3], "current_tz": row[4]}


def normalize_tz(tz_name: str) -> Optional[str]:
    """Каноничное IANA-имя или None. Прощает ввод: пробелы→подчёркивания,
    регистр, «голое» имя города ('bangkok' → 'Asia/Bangkok'). Если городу
    соответствует несколько зон — берём первую по алфавиту (для подсказки
    лучше писать полное имя)."""
    raw = str(tz_name or "").strip().replace(" ", "_")
    if not raw:
        return None
    try:
        return ZoneInfo(raw).key
    except Exception:
        pass
    low = raw.lower()
    for key in sorted(zoneinfo.available_timezones()):
        if key.lower() == low or key.split("/")[-1].lower() == low:
            return key
    return None


def set_current_tz(tz_name: str, person_id: str = SELF_PERSON_ID) -> str:
    """Переключить текущую зону. ValueError с человекочитаемой причиной —
    вызывающие (бот/эндпоинт) показывают её как есть."""
    key = normalize_tz(tz_name)
    if not key:
        raise ValueError(f"не знаю такую зону: {tz_name!r}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {_HEALTH_SCHEMA}.people SET current_tz = %s WHERE id = %s",
            (key, person_id),
        )
        conn.commit()
    timeutil.invalidate_tz_cache()
    logger.info("people: текущая зона %s -> %s", person_id, key)
    return key


def reset_current_tz(person_id: str = SELF_PERSON_ID) -> str:
    """Вернуть домашнюю зону (current_tz = home_tz). Возвращает её имя."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {_HEALTH_SCHEMA}.people SET current_tz = home_tz WHERE id = %s RETURNING home_tz",
            (person_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise ValueError(f"человек {person_id!r} не найден")
    timeutil.invalidate_tz_cache()
    logger.info("people: %s вернулся в домашнюю зону %s", person_id, row[0])
    return row[0]


def is_travelling(person_id: str = SELF_PERSON_ID) -> bool:
    """current_tz != home_tz (для «сегодня · Bangkok» на дашборде)."""
    p = get_person(person_id)
    return bool(p and p["home_tz"] and p["current_tz"] and p["current_tz"] != p["home_tz"])
