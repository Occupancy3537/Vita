"""app/people.py — часовой пояс человека (Фаза 3 плана TIME_AND_MULTIUSER):
валидация IANA-зоны, смена, возврат домой. Тесты меняют current_tz в card_test;
фикстура восстанавливает исходное значение после каждого теста."""
import pytest

from app import people
from app.db import get_conn, schema


@pytest.fixture(autouse=True)
def restore_tz():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT current_tz FROM {schema()}.people WHERE id = 'self'")
        row = cur.fetchone()
    saved = row[0] if row else None
    yield
    if saved is not None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {schema()}.people SET current_tz = %s WHERE id = 'self'", (saved,))
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


def test_set_current_tz_bad_name_raises_and_changes_nothing():
    before = people.get_person()["current_tz"]
    with pytest.raises(ValueError):
        people.set_current_tz("не/зона")
    assert people.get_person()["current_tz"] == before
