"""Сверка нового честного сбора с недостоверным файлом от 2026-09-30 (571 позиция).

Сопоставление — по (lab, нормализованное название) и по URL; НЕ по кодам
(в старом файле коды подозрительны). Метрики + HTTP-статусы старых URL.

    python -m scripts.lab_scrape.compare_old <новый.json> <старый.json> [--check-urls]
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter

import httpx

from .core import norm_name


def keyed_by_name(rows):
    out = {}
    for r in rows:
        key = (r.get("lab_code"), norm_name(r.get("name") or ""))
        out.setdefault(key, []).append(r)
    return out


def keyed_by_url(rows):
    return {(r.get("lab_code"), (r.get("url") or "").rstrip("/")): r for r in rows}


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    check_urls = "--check-urls" in argv
    if len(args) < 2:
        print(__doc__)
        return 2
    new_path, old_path = args[0], args[1]
    new = json.load(open(new_path, encoding="utf-8"))
    old = json.load(open(old_path, encoding="utf-8"))
    print(f"новый сбор: {len(new)} позиций | старый файл: {len(old)} позиций")

    n_by_name, o_by_name = keyed_by_name(new), keyed_by_name(old)
    n_by_url, o_by_url = keyed_by_url(new), keyed_by_url(old)
    inter_name = set(n_by_name) & set(o_by_name)
    inter_url = set(n_by_url) & set(o_by_url)
    print(f"пересечение по (лаба, нормализованное название): {len(inter_name)}")
    print(f"пересечение по URL: {len(inter_url)}")

    price_same = price_diff = 0
    deltas = []
    for k in inter_name:
        p_new = n_by_name[k][0].get("price")
        p_old = o_by_name[k][0].get("price")
        if isinstance(p_new, (int, float)) and isinstance(p_old, (int, float)):
            if abs(p_new - p_old) < 0.01:
                price_same += 1
            else:
                price_diff += 1
                deltas.append({"lab": k[0], "name": (n_by_name[k][0].get("name") or "")[:60],
                               "old": p_old, "new": p_new,
                               "diff": round(p_new - p_old, 2)})
    code_same = sum(
        1 for k in inter_name
        if str(n_by_name[k][0].get("external_code")) == str(o_by_name[k][0].get("external_code")))
    print(f"цены в пересечении: совпали точно {price_same}, различаются {price_diff}")
    print(f"коды в пересечении совпадают: {code_same} из {len(inter_name)}")
    for d in sorted(deltas, key=lambda d: -abs(d["diff"]))[:20]:
        print(f"  Δ {d['diff']:+8.1f} ₽ [{d['lab']}] {d['name']} ({d['old']} → {d['new']})")

    only_old = sorted(set(o_by_name) - set(n_by_name))
    only_new = sorted(set(n_by_name) - set(o_by_name))
    print(f"\nтолько в старом (подозрение на выдумку): {len(only_old)}")
    for lab, nm in only_old[:25]:
        print(f"  [{lab}] {nm[:70]}")
    print(f"только в новом: {len(only_new)}")
    for lab, nm in only_new[:15]:
        print(f"  [{lab}] {nm[:70]}")

    if check_urls:
        s = httpx.Client(headers={"User-Agent": "Mozilla/5.0 VL-personal-noncommercial-price-collect"},
                         timeout=20, follow_redirects=True)
        stats = Counter()
        seen_hosts = {}
        for i, r in enumerate(sorted(old, key=lambda r: r.get("url") or "")):
            url = (r.get("url") or "").strip()
            if not url:
                stats["без URL"] += 1
                continue
            host = url.split("/")[2] if "://" in url else "?"
            wait = 0.7 - (time.monotonic() - seen_hosts.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            status = "ERR"
            try:
                resp = s.head(url)
                if resp.status_code in (405, 501, 403):
                    resp = s.get(url)
                status = str(resp.status_code)
            except httpx.HTTPError as e:
                status = "ERR:" + type(e).__name__
            seen_hosts[host] = time.monotonic()
            stats[status] += 1
            if (i + 1) % 50 == 0:
                print(f"  ...проверено URL: {i + 1}/{len(old)}")
        print("\nHTTP-статусы старых URL:")
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
