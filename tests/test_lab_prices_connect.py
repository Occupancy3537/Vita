"""Этап B подключения цен: нормализация кодов без среза «/», seed --prune
(на card_test), verified в офферах, метка в export_text."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.lab_prices_ingest import parse_rows, prune_stale, upsert
from app.lab_prices import build_export_text, panel_offers, LabItem, PRICES_VERIFIED

LATEST = Path("/home/openclaw/lab_prices/latest.json")


def _row(lab="gemotest", code="1.1", name="Тест", price=100.0):
    return {"lab_code": lab, "lab_name": lab, "city": "Владивосток",
            "external_code": code, "name": name, "category": None, "price": price,
            "currency": "RUB", "turnaround_time": None, "composition": None,
            "url": None, "scraped_at": "2026-09-30T00:00:00+00:00"}


# --- B1: коды как есть -------------------------------------------------------

def test_parse_rows_keeps_slash_codes():
    """«110ГП/БЗ» — настоящий код Инвитро; срез хвоста после «/» убран."""
    rows = parse_rows([_row(lab="invitro", code="110ГП/БЗ"),
                       _row(lab="invitro", code="105/6", name="Другая")])
    assert {r["external_code"] for r in rows} == {"110ГП/БЗ", "105/6"}


def test_latest_json_has_zero_duplicate_keys_after_normalization():
    """На реальном latest.json после нормализации ноль дублей (lab, code)."""
    if not LATEST.exists():
        pytest.skip("latest.json недоступен (вне VPS)")
    rows = json.load(open(LATEST, encoding="utf-8"))
    parsed = parse_rows(rows)
    keys = [(r["lab_key"], r["external_code"]) for r in parsed]
    assert len(keys) == len(set(keys))


# --- B2: prune (card_test) ---------------------------------------------------

@pytest.fixture()
def _seeded_lab():
    from app.db import get_conn, schema
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema()}.lab_item WHERE lab_key = 'prunetest'")
        cur.execute(f"DELETE FROM {schema()}.lab WHERE key = 'prunetest'")
        conn.commit()
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema()}.lab_item WHERE lab_key = 'prunetest'")
        cur.execute(f"DELETE FROM {schema()}.lab WHERE key = 'prunetest'")
        conn.commit()


def test_prune_removes_stale_and_is_idempotent(_seeded_lab):
    """5 строк были, в файле 3 (c0-c2 обновятся, c3-c4 сняты) → удаление 2/5 = 40%,
    ниже порога 60%: prune удаляет снятые и идемпотентен."""
    from app.db import get_conn, schema
    rows5 = parse_rows([_row(lab="prunetest", code=f"c{i}") for i in range(5)])
    with get_conn() as conn, conn.cursor() as cur:
        upsert(cur, rows5)
        conn.commit()
    rows3 = parse_rows([_row(lab="prunetest", code=f"c{i}", price=150.0 + i) for i in range(3)])
    with get_conn() as conn, conn.cursor() as cur:
        upsert(cur, rows3)
        conn.commit()
        removed = prune_stale(cur, rows3)
        conn.commit()
    assert removed == {"prunetest": 2}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT external_code, price_rub FROM {schema()}.lab_item "
                    "WHERE lab_key = 'prunetest' ORDER BY external_code")
        got = cur.fetchall()
        assert [g[0] for g in got] == ["c0", "c1", "c2"]
        assert got[0][1] == 150.0  # цена обновилась
        # идемпотентность: второй запуск удаляет 0
        assert prune_stale(cur, rows3) == {"prunetest": 0}


def test_prune_guard_blocks_over_60_percent(_seeded_lab):
    from app.db import get_conn
    rows3 = parse_rows([_row(lab="prunetest", code=f"d{i}") for i in range(3)])
    with get_conn() as conn, conn.cursor() as cur:
        upsert(cur, rows3)
        conn.commit()
    rows1 = parse_rows([_row(lab="prunetest", code="d0")])
    with get_conn() as conn, conn.cursor() as cur:
        upsert(cur, rows1)
        conn.commit()
        with pytest.raises(RuntimeError, match="не похож на полный прайс"):
            prune_stale(cur, rows1)
        conn.rollback()  # транзакция aborted после ошибки — чистим перед проверкой
        # ничего не удалено
        cur.execute("SELECT count(*) FROM card_test.lab_item WHERE lab_key = 'prunetest'")
        assert cur.fetchone()[0] == 3


# --- B3: verified + метка ----------------------------------------------------

def _items():
    return {"gemotest": [LabItem(lab="gemotest", lab_name="Гемотест",
                                 external_code="1.18", name="Гликированный гемоглобин",
                                 kind="single", price_rub=790.0,
                                 covers=("M031",), parsed_at=datetime(2026, 9, 30, tzinfo=timezone.utc))]}


def test_offers_carry_verified_false_and_collected_at():
    offers = panel_offers(["M031"], _items())
    assert offers, "оффер не построился"
    o = offers[0]
    assert o["verified"] is False
    assert o["verified"] == PRICES_VERIFIED
    assert o["collected_at"] == o["parsed_at"] == "2026-09-30T00:00:00+00:00"


def test_export_text_ends_with_verify_note():
    pick = {"name": "Гемотест", "price_rub": 790.0,
            "parsed_at": "2026-09-30T00:00:00+00:00",
            "breakdown": [{"code": "1.18", "name": "Гликированный гемоглобин",
                           "price_rub": 790.0, "covers": ["M031"], "note": None}],
            "missing": []}
    panel = {"date": "2026-10-05", "n_markers": 1, "tube_types": [], "fasting_required": True}
    text = build_export_text(panel, pick)
    assert text.rstrip().endswith("Цены собраны автоматически 30.09 — проверьте на сайте лаборатории перед оплатой.")
    assert "перед оплатой" in text
