"""app/people.py — часовой пояс человека (Фаза 3 плана TIME_AND_MULTIUSER):
валидация IANA-зоны, смена, возврат домой. app/people.py читает
REGISTRAR_HEALTH_SCHEMA (двойник health.people — не объектная модель
card.*, живёт в health_test, см. tests/conftest.py, ROADMAP 0.7/0.2
2026-09-24: card_test/health_test разведены, чтобы card_test было 1:1
с боевой card). Тесты меняют current_tz там же; фикстура восстанавливает
исходное значение после каждого теста."""
import os

import pytest

from app import people, timeutil
from app.db import get_conn

_HEALTH_SCHEMA = os.environ["REGISTRAR_HEALTH_SCHEMA"]


@pytest.fixture(autouse=True)
def restore_tz():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT current_tz FROM {_HEALTH_SCHEMA}.people WHERE id = 'self'")
        row = cur.fetchone()
    saved = row[0] if row else None
    yield
    if saved is not None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {_HEALTH_SCHEMA}.people SET current_tz = %s WHERE id = 'self'", (saved,))
            conn.commit()


def test_normalize_tz_accepts_iana_and_bare_city():
    assert people.normalize_tz("Asia/Bangkok") == "Asia/Bangkok"
    assert people.normalize_tz(" bangkok ") == "Asia/Bangkok"   # прощаем регистр и пробелы
    assert people.normalize_tz("UTC") == "UTC"


def test_normalize_tz_rejects_garbage():
    assert people.normalize_tz("не зона вовсе !!") is None
    assert people.normalize_tz("") is None


def test_set_and_reset_current_tz():
    home = people.get_person()["home_tz"]
    key = people.set_current_tz("Bangkok")
    assert key == "Asia/Bangkok"
    assert people.get_person()["current_tz"] == "Asia/Bangkok"
    assert people.is_travelling() is True
    back = people.reset_current_tz()
    assert back == home
    assert people.is_travelling() is False


def test_set_current_tz_drops_tz_cache(monkeypatch):
    """T3: смена зоны через /tz или страницу «Настройки» видна сразу, а не через
    TTL кеша (иначе бот минуту отвечал бы старой зоной)."""
    monkeypatch.setenv("TIMEUTIL_TZ_CACHE_SECONDS", "300")
    timeutil.invalidate_tz_cache()
    try:
        before = people.get_person()["current_tz"]
        assert timeutil.person_tz_name() == before   # прочиталось и закешировалось
        people.set_current_tz("Bangkok")
        assert timeutil.person_tz_name() == "Asia/Bangkok"
    finally:
        people.reset_current_tz()
        timeutil.invalidate_tz_cache()


def test_set_current_tz_bad_name_raises_and_changes_nothing():
    before = people.get_person()["current_tz"]
    with pytest.raises(ValueError):
        people.set_current_tz("не/зона")
    assert people.get_person()["current_tz"] == before
