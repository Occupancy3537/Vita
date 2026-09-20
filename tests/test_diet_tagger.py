"""app/diet_tagger.py — порт n8n `Diet Quality Tagger` (2026-09-20, группа 2).
health.meals — реальная прод-таблица, юниты на pick_untagged/write_tags бьют
по ней (вставляем/чистим тестовую строку с заведомо непохожим на реальные
Entry_ID числом, не трогаем настоящие приёмы пищи)."""
import pytest

from app import diet_tagger as dt
from app.db import get_conn

TEST_ENTRY_ID = "999999901"


@pytest.fixture(autouse=True)
def cleanup_test_meal():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        conn.commit()


def _insert_test_meal(description: str, nova=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO health.meals ("Entry_ID", "Date", "Meal_description", "NOVA") '
            "VALUES (%s, now(), %s, %s) ON CONFLICT (\"Entry_ID\") DO UPDATE SET "
            '"Meal_description" = EXCLUDED."Meal_description", "NOVA" = EXCLUDED."NOVA"',
            (TEST_ENTRY_ID, description, nova),
        )
        conn.commit()


# --- parse_tags ---------------------------------------------------------------

def test_parse_tags_valid_json():
    raw = '{"NOVA":2,"veg_g":50,"fruit_g":0,"wholegrain_g":30,"legume_nut_g":0,"redmeat_g":0,"ssb_ml":0,"pufa_g":5,"plants":"морковь, лук"}'
    tags = dt.parse_tags(raw)
    assert tags["NOVA"] == 2
    assert tags["veg_g"] == 50.0
    assert tags["plants"] == "морковь, лук"


def test_parse_tags_strips_markdown_fences():
    raw = '```json\n{"NOVA":1,"veg_g":0,"fruit_g":0,"wholegrain_g":0,"legume_nut_g":0,"redmeat_g":0,"ssb_ml":0,"pufa_g":0,"plants":""}\n```'
    tags = dt.parse_tags(raw)
    assert tags["NOVA"] == 1


def test_parse_tags_no_json_returns_empty():
    assert dt.parse_tags("извините, не могу это классифицировать") == {}


def test_parse_tags_malformed_json_returns_empty():
    assert dt.parse_tags("{NOVA: broken}") == {}


def test_parse_tags_missing_nova_returns_empty():
    assert dt.parse_tags('{"veg_g": 10}') == {}


def test_parse_tags_clamps_nova_to_1_4():
    assert dt.parse_tags('{"NOVA": 9, "plants": ""}')["NOVA"] == 4
    assert dt.parse_tags('{"NOVA": -3, "plants": ""}')["NOVA"] == 1


def test_parse_tags_nova_zero_falls_back_to_1():
    """JS: Math.round(Number(j.NOVA) || 1) — 0 сам по себе falsy, откат на 1."""
    assert dt.parse_tags('{"NOVA": 0, "plants": ""}')["NOVA"] == 1


def test_parse_tags_plants_truncated_to_300_chars():
    long_plants = "растение, " * 50
    tags = dt.parse_tags(f'{{"NOVA":1,"plants":"{long_plants}"}}')
    assert len(tags["plants"]) <= 300


def test_parse_tags_missing_numeric_fields_default_to_zero():
    tags = dt.parse_tags('{"NOVA": 1}')
    assert tags["veg_g"] == 0.0
    assert tags["plants"] == ""


# --- pick_untagged (реальная health.meals) -----------------------------------

def test_pick_untagged_includes_test_row_without_nova():
    _insert_test_meal("тестовое блюдо для test_diet_tagger", nova=None)
    with get_conn() as conn, conn.cursor() as cur:
        rows = dt.pick_untagged(cur)
    ids = [eid for eid, _ in rows]
    assert TEST_ENTRY_ID in ids


def test_pick_untagged_excludes_already_tagged_row():
    _insert_test_meal("тестовое блюдо уже с тегом", nova="2")
    with get_conn() as conn, conn.cursor() as cur:
        rows = dt.pick_untagged(cur)
    ids = [eid for eid, _ in rows]
    assert TEST_ENTRY_ID not in ids


def test_pick_untagged_respects_limit():
    with get_conn() as conn, conn.cursor() as cur:
        rows = dt.pick_untagged(cur, limit=2)
    assert len(rows) <= 2


# --- write_tags -----------------------------------------------------------

def test_write_tags_updates_real_row():
    _insert_test_meal("тестовое блюдо для записи тегов", nova=None)
    tags = {"NOVA": 3, "veg_g": 10.0, "fruit_g": 0.0, "wholegrain_g": 0.0,
            "legume_nut_g": 0.0, "redmeat_g": 0.0, "ssb_ml": 0.0, "ПНЖ": 0.0, "plants": "лук"}
    with get_conn() as conn, conn.cursor() as cur:
        dt.write_tags(cur, TEST_ENTRY_ID, tags)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "NOVA", "veg_g", "plants" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        row = cur.fetchone()
    assert row == ("3", "10.0", "лук")


# --- call_model / run_once (мокаем HTTP, не бьём по реальному OpenRouter) ---

def test_call_model_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert dt.call_model("что угодно") == {}


def test_call_model_http_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_post(*a, **kw):
        raise Exception("сеть легла")

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    assert dt.call_model("что угодно") == {}


def test_run_once_tags_and_returns_count(monkeypatch):
    _insert_test_meal("тестовое блюдо для run_once", nova=None)
    monkeypatch.setattr(dt, "pick_untagged", lambda cur, limit=dt.BATCH_SIZE: [(TEST_ENTRY_ID, "тест")])
    monkeypatch.setattr(dt, "call_model", lambda desc: {
        "NOVA": 1, "veg_g": 0.0, "fruit_g": 0.0, "wholegrain_g": 0.0,
        "legume_nut_g": 0.0, "redmeat_g": 0.0, "ssb_ml": 0.0, "ПНЖ": 0.0, "plants": "",
    })
    n = dt.run_once()
    assert n == 1
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "NOVA" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        assert cur.fetchone() == ("1",)


def test_run_once_skips_row_when_model_returns_nothing(monkeypatch):
    _insert_test_meal("тестовое блюдо без ответа модели", nova=None)
    monkeypatch.setattr(dt, "pick_untagged", lambda cur, limit=dt.BATCH_SIZE: [(TEST_ENTRY_ID, "тест")])
    monkeypatch.setattr(dt, "call_model", lambda desc: {})
    n = dt.run_once()
    assert n == 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "NOVA" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        assert cur.fetchone() == (None,)
