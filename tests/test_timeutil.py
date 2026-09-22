"""Фаза 0 плана TIME_AND_MULTIUSER_PLAN_2026-09-22 — app/timeutil.py, единый
источник правила «какой день у человека» (находки T1/T2/T3 аудита логики).

Зона читается из {HEALTH_SCHEMA}.people (в тестах — card_test, см. conftest.py);
ключевые свойства: дефолтная зона при любой проблеме (fail-safe) и «день» в
зоне человека, а не в UTC (раньше CURRENT_DATE в SQL давал вчерашнюю дату до
10:00 по Владивостоку — живое доказательство в AGENT_SYNC #57)."""
from datetime import date, datetime, timedelta, timezone
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


# --- Фаза 3: расписания в зоне человека (next_occurrence / sleep_until_local) ---

def test_next_occurrence_daily_before_and_after_target_hour():
    tz = ZoneInfo("Asia/Vladivostok")
    # 22.09 10:00 VL (00:00 UTC) — сегодняшние 11:00 ещё впереди
    assert timeutil.next_occurrence(11, 0, tz=tz,
                                    now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)) \
        == datetime(2026, 9, 22, 11, 0, tzinfo=tz)
    # 22.09 19:00 VL (09:00 UTC) — уже позже цели, значит завтра
    assert timeutil.next_occurrence(11, 0, tz=tz,
                                    now=datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)) \
        == datetime(2026, 9, 23, 11, 0, tzinfo=tz)


def test_next_occurrence_weekly_sunday():
    tz = ZoneInfo("Asia/Vladivostok")
    base = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc).astimezone(tz)
    nxt = timeutil.next_occurrence(20, 0, weekday=6, tz=tz, now=base)
    assert nxt.weekday() == 6 and nxt > base
    # в найденное воскресенье утром — цель сегодня 20:00
    sunday_morning = nxt.replace(hour=0, minute=0)
    assert timeutil.next_occurrence(20, 0, weekday=6, tz=tz, now=sunday_morning) \
        == sunday_morning.replace(hour=20)
    # а в 21:00 того же воскресенья — уже следующая неделя
    assert timeutil.next_occurrence(20, 0, weekday=6, tz=tz, now=sunday_morning.replace(hour=21)) \
        == sunday_morning.replace(hour=20) + timedelta(days=7)


def test_next_occurrence_first_of_month():
    tz = ZoneInfo("Asia/Vladivostok")
    # 22.09 12:00 VL → ближайшее 1-е число 10:00
    assert timeutil.next_occurrence(10, 0, day_of_month=1, tz=tz,
                                    now=datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)) \
        == datetime(2026, 10, 1, 10, 0, tzinfo=tz)
    # 1-е число 09:00 VL — цель сегодня в 10:00
    assert timeutil.next_occurrence(10, 0, day_of_month=1, tz=tz,
                                    now=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)) \
        == datetime(2026, 10, 1, 10, 0, tzinfo=tz)


def test_sleep_until_local_chunks_and_recomputes(monkeypatch):
    """Сон кусками: после длинного куска цель пересчитывается (смена зоны /tz
    подхватывается), короткий остаток досыпается одним куском и выход."""
    calls = []

    def fake_next(*a, **k):
        delta = 1800 if not calls else 1
        return datetime.now(timezone.utc) + timedelta(seconds=delta)

    monkeypatch.setattr(timeutil, "next_occurrence", fake_next)
    monkeypatch.setattr(timeutil.time, "sleep", lambda s: calls.append(round(s)))
    timeutil.sleep_until_local(11, 0)
    assert calls == [600, 1]

