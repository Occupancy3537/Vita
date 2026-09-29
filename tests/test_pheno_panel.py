"""Атомарность панели PhenoAge (правило одного дня, Влад 2026-09-29):
девятка маркеров Levine едет ОДНИМ забором — не разносится окнами,
не подрезается лимитом на забор, якорится на самый ранний срок.

Тесты чистые: _build_panels не ходит в БД. psycopg стабится на случай
запуска без установленного драйвера (в тест-среде проекта — настоящий)."""
import sys
import types
from datetime import date, timedelta

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        fake_sql = types.SimpleNamespace(SQL=lambda q: q, Identifier=lambda s: s)
        sys.modules["psycopg"] = types.SimpleNamespace(sql=fake_sql)

from app.lab_catalog import LAB_CATALOG, PHENOAGE_PANEL_MARKERS
from app.lab_optimizer import DueItem, _build_panels

TODAY = date(2026, 9, 29)
PHENO = sorted(PHENOAGE_PANEL_MARKERS)  # M003 M004 M008 M017 M024 M039 M043 M049 M062
FILLERS = ["M001", "M002", "M005", "M006", "M007", "M009", "M010", "M011",
           "M012", "M013", "M014", "M015", "M016", "M019", "M020", "M031", "M032", "M081"]


def _items(codes, due, source="standing"):
    return [DueItem(c, due, source, None, LAB_CATALOG.get(c, {}).get("purpose", "")) for c in codes]


def _panel_codes(panel):
    return [m["code"] for m in panel["markers"]]


def test_pheno_stays_whole_under_overflow():
    """9 PhenoAge + 12 других, все due сегодня: панель 1 = 9 PhenoAge + 3 других,
    ни один PhenoAge-маркер не уезжает в панель+2."""
    items = _items(PHENO, TODAY) + _items(FILLERS, TODAY)
    panels, _, _ = _build_panels(items, TODAY, 180, 12, 14)
    p1 = set(_panel_codes(panels[0]))
    assert set(PHENO) <= p1, f"PhenoAge разъехался: {sorted(set(PHENO) - p1)}"
    assert len(p1) == 12


def test_pheno_anchors_to_earliest_due():
    """Разные сроки внутри девятки — вся девятка едет с самым ранним из них."""
    dues = [TODAY, TODAY + timedelta(days=3), TODAY + timedelta(days=10)]
    pheno_items = [DueItem(c, dues[i % 3], "standing", None, "") for i, c in enumerate(PHENO)]
    panels, _, _ = _build_panels(list(pheno_items), TODAY, 180, 12, 14)
    all_codes = [c for p in panels for c in _panel_codes(p)]
    pheno_panels = {i for i, p in enumerate(panels) if set(PHENO) & set(_panel_codes(p))}
    assert len(pheno_panels) == 1, f"девятка в {len(pheno_panels)} панелях"
    assert sorted(c for c in all_codes if c in set(PHENO)) == PHENO


def test_pheno_not_pulled_early_into_other_window():
    """Другие маркеры due сегодня, PhenoAge — через 90 дней: девятка НЕ тянется
    в сегодняшнюю панель, едет своей датой."""
    items = _items(FILLERS, TODAY) + _items(PHENO, TODAY + timedelta(days=90))
    panels, _, _ = _build_panels(items, TODAY, 180, 12, 14)
    p1 = set(_panel_codes(panels[0]))
    assert not (set(PHENO) & p1), "PhenoAge притащили раньше срока ради одного забора"


def test_pheno_not_split_across_panels():
    """12 других + 9 PhenoAge в одном окне — лимит 12: PhenoAge целиком в панели 1,
    лишние ДРУГИЕ едут в панель+2 (30 дней), а не наоборот."""
    items = _items(FILLERS[:12], TODAY) + _items(PHENO, TODAY)
    panels, _, _ = _build_panels(items, TODAY, 180, 12, 14)
    assert len(panels) == 2
    p1 = set(_panel_codes(panels[0]))
    p2 = set(_panel_codes(panels[1]))
    assert set(PHENO) <= p1
    assert not (set(PHENO) & p2)
    assert len(p1) == 12 and len(p2) == 9
    assert panels[1]["markers"][0]["natural_due_date"] == (TODAY + timedelta(days=30)).isoformat()
