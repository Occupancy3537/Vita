
import json
from pathlib import Path

import pytest

from app.lab_prices_map import COVERS
from app.lab_catalog import LAB_CATALOG

LATEST = Path("/home/openclaw/lab_prices/latest.json")


def _latest_keys():
    if not LATEST.exists():
        pytest.skip("latest.json недоступен (вне VPS)")
    rows = json.load(open(LATEST, encoding="utf-8"))
    return {(r["lab_code"], r["external_code"]) for r in rows}


def test_all_mapping_keys_exist_in_latest():
    keys = _latest_keys()
    missing = [k for k in COVERS if k not in keys]
    assert not missing, f"сироты (ключей нет в прайсе): {missing[:10]}"


def test_all_markers_are_catalog_codes():
    bad = {m for ms in COVERS.values() for m in ms if m not in LAB_CATALOG}
    assert not bad, f"маркеры вне каталога: {bad}"


def test_no_duplicate_markers_within_position():
    for k, ms in COVERS.items():
        assert len(ms) == len(set(ms)), f"дубли внутри позиции {k}: {ms}"


def test_control_table_cells_without_position_have_no_coverage():
    """Сверка с эталоном Влада: где контрольная таблица говорит «нет в собранном
    прайсе» — покрытия быть не должно."""
    ct_path = Path("/home/openclaw/lab_prices/reports/control_table.csv")
    if not ct_path.exists():
        pytest.skip("control_table.csv недоступен (вне VPS)")
    import csv
    ct = list(csv.DictReader(open(ct_path, encoding="utf-8-sig"), delimiter=";"))
    bad = [(r["маркер"], r["лаба"]) for r in ct
           if "нет в собранном прайсе" in r.get("цена", "")
           and (r["лаба"], r["код"]) in COVERS]
    # ПТИ Гемотеста закрывается 6.5/6.10 (ПВ+ПТИ/МНО+ПТИ) — сознательное
    # решение, не «нет»; исключения перечислены здесь явно.
    allowed = {("ПТИ", "gemotest")}
    bad = [b for b in bad if (b[1], b[0]) not in allowed and b not in allowed]
    assert not bad, f"таблица говорит «нет», а покрытие есть: {bad}"
