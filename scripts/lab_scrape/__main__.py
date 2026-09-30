"""CLI сборщика: python -m scripts.lab_scrape run [--labs ...] [--out DIR] [--dry]

run: сбор → валидация → файлы (JSON/rejected/meta/raw) → ворота качества →
     дифф к предыдущему прошедшему запуску. При --dry latest.json не
     обновляется и публикация не выполняется (публикация — отдельный шаг
     publish.sh, который запускает человек).

Идемпотентность: повторный запуск в тот же день ПЕРЕЗАПИСЫВАЕТ файлы в
runs/<день>/ целиком (свежие файлы вместо старых), ничего не дописывает.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone

from . import DELAY_SECONDS, UA, VERSION, LAB_NAMES
from . import core, gemotest, invitro, tafi, unilab

DEFAULT_BASE = "/home/openclaw/lab_prices"
COLLECTORS = {"gemotest": gemotest, "invitro": invitro, "tafi": tafi, "unilab": unilab}


def vladivostok_today() -> str:
    """Дата запуска по Владивостоку (город сбора) — UTC+10, без DST."""
    return (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")


def git_sha() -> str:
    try:
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def diff_report(rows, prev) -> dict:
    """Дифф к предыдущему прошедшему запуску (по lab+code)."""

    def keyed(rs):
        return {(r["lab_code"], r["external_code"]): r for r in rs}

    cur, old = keyed(rows), keyed(prev or [])
    added = sorted(set(cur) - set(old))
    removed = sorted(set(old) - set(cur))
    price_changed = []
    for k in sorted(set(cur) & set(old)):
        p_old, p_new = old[k].get("price"), cur[k].get("price")
        if isinstance(p_old, (int, float)) and isinstance(p_new, (int, float)) and p_old != p_new:
            price_changed.append({"lab": k[0], "code": k[1], "name": cur[k]["name"][:60],
                                  "old": p_old, "new": p_new})
    return {"added": len(added), "removed": len(removed),
            "price_changed": len(price_changed),
            "price_changed_top": sorted(price_changed, key=lambda d: -abs(d["new"] - d["old"]))[:20]}


def run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":       # CLI: python -m scripts.lab_scrape run [...]
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="scripts.lab_scrape")
    ap.add_argument("--labs", nargs="*", default=sorted(COLLECTORS), choices=sorted(COLLECTORS))
    ap.add_argument("--out", default=None, help="каталог запуска (по умолчанию <base>/runs/<день ВЛ>)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--dry", action="store_true", help="не обновлять latest.json и не публиковать")
    args = ap.parse_args(argv)

    day = vladivostok_today()
    run_dir = args.out or os.path.join(args.base, "runs", day)
    os.makedirs(run_dir, exist_ok=True)
    raw_dir = os.path.join(run_dir, "raw")
    # идемпотентность: fresh raw/ на каждый запуск
    for f in os.listdir(raw_dir) if os.path.isdir(raw_dir) else []:
        os.unlink(os.path.join(raw_dir, f))
    started = core.now_iso()

    prev_path = os.path.join(args.base, "latest.json")
    prev_rows = load_json(prev_path) if not args.out else None
    if not isinstance(prev_rows, list):
        prev_rows = []

    log_path = (os.path.join(run_dir, "scrape.log") if args.out
                else os.path.join(args.base, "scrape.log"))

    def log(msg: str):
        line = f"{core.now_iso()} {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    log(f"=== запуск {VERSION} (git {git_sha()}) labs={args.labs} dry={args.dry} -> {run_dir}")

    all_rows: list[dict] = []
    rejects: list[dict] = []
    per_lab = {}
    session = core.new_session()
    for lab in args.labs:
        stats = {"requests": 0, "found": 0, "accepted": 0, "rejected": 0}
        lab_rejects: list[dict] = []
        try:
            rows = COLLECTORS[lab].collect(session, run_dir, stats, lab_rejects)
        except Exception as e:
            log(f"[{lab}] ИСКЛЮЧЕНИЕ: {type(e).__name__}: {e}")
            stats["exception"] = f"{type(e).__name__}: {e}"[:300]
            rows = []
        for r in rows:
            r.setdefault("lab_code", lab)
        all_rows.extend(rows)
        rejects.extend([dict(d, lab=d.get("lab", lab)) for d in lab_rejects])
        stats["rejected"] = len(lab_rejects)
        per_lab[lab] = stats
        log(f"[{lab}] найдено {stats['found']}, принято {len(rows)}, отклонено {stats['rejected']}, "
            f"HTTP-запросов {stats['requests']}")

    # ворота качества
    problems = core.validate_rows(all_rows)
    zero_labs = [lab for lab in args.labs if not any(r["lab_code"] == lab for r in all_rows)]
    if zero_labs:
        problems.append(f"лабы вернули 0 позиций: {', '.join(zero_labs)}")
    problems += core.compare_with_previous(all_rows, prev_rows)
    gates = {"passed": not problems, "reasons": problems[:50], "previous_run": prev_path if prev_rows else None}
    if problems:
        for p in problems:
            log("GATE: " + p)

    # файлы запуска (перезапись целиком)
    out_json = os.path.join(run_dir, f"lab_prices_vladivostok_{day}.json")
    all_rows.sort(key=lambda r: (r["lab_code"], r["external_code"]))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=1)
    with open(os.path.join(run_dir, "rejected.json"), "w", encoding="utf-8") as f:
        json.dump(rejects, f, ensure_ascii=False, indent=1)
    meta = {
        "version": VERSION, "git_sha": git_sha(),
        "started_at": started, "finished_at": core.now_iso(),
        "run_dir": run_dir, "out_file": os.path.basename(out_json),
        "user_agent": UA, "delay_seconds": DELAY_SECONDS,
        "labs": {lab: {**per_lab.get(lab, {})} for lab in args.labs},
        "totals": {"rows": len(all_rows), "rejected": len(rejects)},
        "gates": gates,
        "diff_vs_previous": diff_report(all_rows, prev_rows) if prev_rows else None,
        "notes": {
            "tafi": "external_code = слаг карточки (артикул в статике сайта не отдаётся); "
                    "raw_ref указывает на источник цены (/prices/), сырые ответы обогащения — тоже в raw/",
            "gemotest": "цена/код/название — из data-атрибутов карточек на страницах групп; "
                        "срок/состав — обогащение с карточек комплексов (raw в raw/)",
            "invitro": "источник — golk JSON-API с cityID Владивостока (доказательство: raw/cities); "
                       "url построен по паттерну и выборочно проверен (см. labs.invitro.url_pattern_checks)",
            "unilab": "источник — карточки по официальным sitemap города; каждый ответ сохранён в raw/",
        },
    }
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    if gates["passed"] and not args.dry:
        link = os.path.join(args.base, "latest.json")
        tmp = link + ".tmp"
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.unlink(tmp)
        os.symlink(out_json, tmp)
        os.replace(tmp, link)
        log(f"latest.json -> {out_json}")
    elif args.dry:
        log("dry-режим: latest.json не тронут, публикация не выполнялась")
    else:
        log("ворота НЕ пройдены: latest.json не обновлён")

    log(f"итог: строк {len(all_rows)}, отклонено {len(rejects)}, "
        f"ворота {'пройдены' if gates['passed'] else 'ПРОВАЛЕНЫ'}")
    return 0 if gates["passed"] else 3


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
