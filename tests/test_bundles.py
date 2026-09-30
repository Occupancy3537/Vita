"""Клинические связки (2026-09-30, живая жалоба Влада: фолат сдавался без B12 и
гомоцистеина). Чистые тесты _build_panels — без БД. Связка едет ОДНИМ забором,
не режется лимитом и при нехватке места уезжает целиком."""
import sys
import types
from datetime import date, timedelta

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        sys.modules["psycopg"] = types.SimpleNamespace(
            sql=types.SimpleNamespace(SQL=lambda q: q, Identifier=lambda s: s))

from app.lab_catalog import CLINICAL_BUNDLES, LAB_CATALOG, PHENOAGE_PANEL_MARKERS
from app.lab_optimizer import DEFAULT_MAX_PER_DRAW, DueItem, _build_panels

TODAY = date(2026, 9, 30)
PHENO = sorted(PHENOAGE_PANEL_MARKERS)
BVIT = ["M033", "M034", "M080"]
LIPIDS = ["M009", "M010", "M011", "M012", "M013", "M014", "M078"]


def _di(code, due, source="standing", reason=""):
    return DueItem(code, due, source, None, reason)


def _codes(panel):
    return {m["code"] for m in panel["markers"]}


def _plan(items, max_per_draw=12):
    return _build_panels(items, TODAY, 180, max_per_draw, 14)[0]


def _singles(n):
    return [_di(f"T{i:03d}", TODAY) for i in range(n)]


def test_default_limit_is_20():
    assert DEFAULT_MAX_PER_DRAW == 20


def test_bundles_are_disjoint_valid_and_exclude_phenoage():
    seen = {}
    for b in CLINICAL_BUNDLES:
        for c in b["codes"]:
            assert c in LAB_CATALOG, f"{b['key']}: {c} нет в каталоге"
            assert c not in seen, f"{c} в двух связках: {seen[c]} и {b['key']}"
            assert c not in PHENOAGE_PANEL_MARKERS, f"{c} — PhenoAge, не должен быть в связке"
            seen[c] = b["key"]


def test_b12_folate_homocysteine_ride_together_when_all_due():
    """Живой случай Влада: все три просрочены с 2020 — раньше фолат уезжал один."""
    items = [_di("M033", date(2020, 6, 17)), _di("M034", date(2020, 6, 17)), _di("M080", TODAY)] + _singles(12)
    panels = _plan(items, max_per_draw=5)
    holder = [i for i, p in enumerate(panels) if _codes(p) & set(BVIT)]
    assert len(holder) == 1, "тройка разорвана между панелями"
    assert set(BVIT) <= _codes(panels[holder[0]])


def test_bundle_not_cut_by_limit_others_go_first_to_overflow():
    """PhenoAge (9) + липиды (7) + одиночки при лимите 12: свободно 3 — липиды (7) не влезают
    и уезжают ЦЕЛИКОМ на +30, одиночки заполняют остаток."""
    items = [_di(c, TODAY) for c in PHENO] + [_di(c, TODAY) for c in LIPIDS] + _singles(3)
    panels = _plan(items, max_per_draw=12)
    p1 = _codes(panels[0])
    assert set(PHENO) <= p1
    assert not (p1 & set(LIPIDS)), "часть липидов осталась в панели 1"
    lipid_panels = [i for i, p in enumerate(panels) if _codes(p) & set(LIPIDS)]
    assert len(lipid_panels) == 1 and set(LIPIDS) <= _codes(panels[lipid_panels[0]])
    assert date.fromisoformat(panels[lipid_panels[0]]["date"]) >= TODAY + timedelta(days=30)


def test_doctor_assigned_outranks_bundle_when_space_is_short():
    items = ([_di(c, TODAY) for c in BVIT] + [_di("T900", TODAY, source="recommendation", reason="врач")] +
             _singles(2))
    panels = _plan(items, max_per_draw=3)
    assert "T900" in _codes(panels[0])  # назначенное врачом раньше связки
    assert not (_codes(panels[0]) & set(BVIT)) or set(BVIT) <= _codes(panels[0])


def test_cbc_bundle_counts_as_one_unit_of_the_limit():
    cbc = next(b for b in CLINICAL_BUNDLES if b["key"] == "cbc")["codes"]
    items = [_di(c, TODAY) for c in cbc] + _singles(11)
    p1 = _plan(items, max_per_draw=12)[0]
    assert set(cbc) <= _codes(p1), "ОАК-связка разрезана"
    assert len(_codes(p1)) == len(cbc) + 11  # ОАК = 1 единица, влезли ещё 11 одиночек


def test_near_due_member_is_pulled_far_one_is_not():
    """B12 сегодня, фолат через 20 дней — едут вместе; ферритин сегодня, железо через 300 — нет."""
    items = [_di("M034", TODAY), _di("M033", TODAY + timedelta(days=20)),
             _di("M027", TODAY), _di("M028", TODAY + timedelta(days=300))]
    panels = _plan(items, max_per_draw=12)
    p1 = _codes(panels[0])
    assert {"M034", "M033"} <= p1
    assert "M027" in p1 and "M028" not in p1


def test_bundle_alone_is_not_wrapped_when_single_member():
    panels = _plan([_di("M033", TODAY)] + _singles(2))
    assert "M033" in _codes(panels[0])


def test_phenoage_still_atomic_and_first():
    items = [_di(c, TODAY) for c in PHENO] + [_di(c, TODAY) for c in BVIT] + _singles(20)
    panels = _plan(items, max_per_draw=12)
    assert set(PHENO) <= _codes(panels[0])
    assert set(BVIT) <= _codes(panels[0])  # 9 + 3 = 12 — связка (rank 2) раньше одиночек (rank 3)


def test_plan_is_deterministic():
    items = [_di(c, TODAY) for c in PHENO + BVIT + LIPIDS] + _singles(5)
    a = _plan(items, 12)
    b = _plan(list(reversed(items)), 12)
    assert [(_p["date"], sorted(_codes(_p))) for _p in a] == [(_p["date"], sorted(_codes(_p))) for _p in b]


def test_shape_doctor_collapses_cbc_rows_into_one_analysis():
    """ОАК — один анализ: 26 показателей связки схлопываются в одну строку, число анализов честное."""
    from app import vita
    cbc = next(b for b in CLINICAL_BUNDLES if b["key"] == "cbc")["codes"]
    plan = {"panels": [{"date": "2026-10-01", "n_markers": len(cbc) + 1, "fasting_required": False,
                        "tube_types": [], "shifted": [],
                        "markers": [{"code": c, "name": LAB_CATALOG[c]["name"], "why": "", "category": "ОАК",
                                     "fasting_required": False, "source_type": "standing",
                                     "natural_due_date": "2026-10-01"} for c in cbc] +
                                   [{"code": "M035", "name": "Витамин D", "why": "", "category": "Дефициты",
                                     "fasting_required": False, "source_type": "standing",
                                     "natural_due_date": "2026-10-01"}]}], "conflicts": []}
    nd = vita.shape_doctor([], [], [], plan)["next_draw"]
    assert nd["n"] == 2  # ОАК + витамин D
    assert sum(1 for m in nd["markers"] if "Общий анализ крови" in m["name"]) == 1
