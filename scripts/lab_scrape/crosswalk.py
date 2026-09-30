"""Предложение crosswalk: старые ключи COVERS → позиции нового сбора.

Для каждого ключа (lab, external_code) из app.lab_prices_map.COVERS берём
список кодов каталога, которые он закрывает, их КАНОНИЧЕСКИЕ названия из
app.lab_catalog.LAB_CATALOG и ищем в новом сборе позицию той же лабы по
ТОЧНОМУ совпадению нормализованного названия. Fuzzy не применяется.

    python -m scripts.lab_scrape.crosswalk <новый.json> --csv crosswalk_proposal.csv

Выход: old_key, candidate_new_key, основание (коды каталога + имя),
уверенность exact/ambiguous/none. app/lab_prices_map.py не меняется —
это ПРЕДЛОЖЕНИЕ для проверки Claude/Влада.
"""
from __future__ import annotations

import csv
import json
import sys

from .core import norm_name


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    csv_out = None
    if "--csv" in argv:
        csv_out = argv[argv.index("--csv") + 1]
    if not args:
        print(__doc__)
        return 2

    sys.path.insert(0, "/home/openclaw/longevity-project/card-service")
    from app.lab_catalog import LAB_CATALOG
    from app.lab_prices_map import COVERS

    rows = json.load(open(args[0], encoding="utf-8"))
    by_lab: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_lab.setdefault(r["lab_code"], {})[norm_name(r["name"])] = r

    out = []
    for (lab, old_code), mcodes in sorted(COVERS.items()):
        names = [LAB_CATALOG[m]["name"] for m in mcodes if m in LAB_CATALOG]
        cands: dict[str, dict] = {}
        for nm in names:
            hit = by_lab.get(lab, {}).get(norm_name(nm))
            if hit:
                cands[hit["external_code"]] = hit
        if len(cands) == 1:
            new_code, hit = next(iter(cands.items()))
            confidence = "exact"
        elif len(cands) > 1:
            new_code = ";".join(sorted(cands))
            hit = None
            confidence = "ambiguous"
        else:
            new_code, hit, confidence = "", None, "none"
        basis = f"{'+'.join(mcodes)}: {'; '.join(names[:3])}{'…' if len(names) > 3 else ''}"
        out.append({
            "old_key": f"{lab}|{old_code}",
            "candidate_new_key": f"{lab}|{new_code}" if new_code else "",
            "confidence": confidence,
            "basis": basis,
            "new_name": (hit or {}).get("name", ""),
            "new_price": (hit or {}).get("price", ""),
            "new_url": (hit or {}).get("url", ""),
        })

    if csv_out:
        with open(csv_out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(out)
        print(f"CSV: {csv_out}")
    from collections import Counter
    print("уверенность:", dict(Counter(o["confidence"] for o in out)))
    for o in out:
        if o["confidence"] != "exact":
            print(f"  {o['old_key']:24} [{o['confidence']:9}] {o['basis'][:70]} → {o['candidate_new_key']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
