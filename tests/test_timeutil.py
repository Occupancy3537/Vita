"""Фаза 0 плана TIME_AND_MULTIUSER_PLAN_2026-09-22 — app/timeutil.py, единый
источник правила «какой день у человека» (находки T1/T2/T3 аудита логики).

Зона читается из {HEALTH_SCHEMA}.people (в тестах — card_test, см. conftest.py);
ключевые свойства: дефолтная зона при любой проблеме (fail-safe) и «день» в
зоне человека, а не в UTC (раньше CURRENT_DATE в SQL давал вчерашнюю дату до
10:00 по Владивостоку — живое доказательство в AGENT_SYNC #57)."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import timeutil


def test_person_tz_returns_self_zone():
    assert str(timeutil.person_tz()) == "Asia/Vladivostok"


def test_person_tz_unknown_person_falls_back_to_default():
    """Нет строки в people — не исключение, а дефолтная зона."""
    assert str(timeutil.person_tz("nobody-xyz")) == timeutil.DEFAULT_TZ


def test_person_tz_unknown_zone_name_falls_back(monkeypatch):
    """Битая IANA-строка в БД тоже не должна ронять вызов."""
    monkeypatch.setattr(timeutil, "_read_tz_name", lambda person_id: "Mars/Olympus")
    assert str(timeutil.person_tz()) == timeutil.DEFAULT_TZ


def test_today_is_vladivostok_date():
    expected = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Vladivostok")).date()
    assert timeutil.today() == expected


def test_local_day_uses_person_zone_not_utc():
    # 23.09 00:30 VL == 22.09 14:30 UTC → VL-день 23.09 (UTC-дата была бы 22-е)
    assert timeutil.local_day(datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)) == date(2026, 9, 23)
    # 22.09 23:45 VL == 22.09 13:45 UTC → VL-день 22.09
    assert timeutil.local_day(datetime(2026, 9, 22, 13, 45, tzinfo=timezone.utc)) == date(2026, 9, 22)


def test_local_day_naive_treated_as_utc():
    assert timeutil.local_day(datetime(2026, 9, 22, 14, 30)) == date(2026, 9, 23)
