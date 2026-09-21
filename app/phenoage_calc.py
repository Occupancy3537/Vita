"""Порт n8n `PhenoAge Calc` (2026-09-21, найден и перенесён при проверке
«можно ли полностью убрать n8n») — формула биологического возраста Levine
et al. 2018 (Aging, Albany NY). Раз в неделю (воскресенье, 09:00 ВЛ —
подтверждено сверкой с execution_entity ниже, тот же способ, что уже
использовался для Anomaly_Detector/Weekly Advisor) пересчитывает
health.phenoage_log из последних лабораторных данных.

НАЙДЕНО, ИСПРАВЛЕНО (не 1:1 перенос, сознательно): оригинал читал Results/
Markers/Visits из ТРЕТЬЕЙ книги Google Sheets (документ "phenoage",
1vtbObHKfiaXSLCYwhPkLj5M0qf4GR286GWWZgWXRYbY) — но эти же данные УЖЕ
дублируются в health.results/health.markers/health.visits, и card-service
(доктор/регистратор) уже какое-то время пишет НОВЫЕ визиты/результаты
напрямую в Postgres без обратной записи в Sheets (тот же принцип "PG-only
вперёд", что уже применялся к lab_plan, recommendations_log, Anomalies_Log
в этой миграции). Значит Sheets-источник PhenoAge Calc уже мог быть СТАРЕЕ
Postgres для всего, что пришло через доктора после ~17.09 — не "такой же
источник, другой протокол", а потенциально устаревший источник. Порт читает
health.results/markers/visits (Postgres, канон), не Sheets — это фикс, не
только перенос.

PhenoAge_Config (справочник эталонных значений маркеров + версия единиц
CRP) остаётся в Google Sheets — низкочастотное (правится вручную изредка)
чтение, заводить для него Postgres-путь не по бюджету сложности (тот же
принцип, что Metric_Config в app/anomaly_detector.py).

РЕШЕНИЕ (не спрашивал отдельно — тот же прецедент, что уже принят и
одобрен для recommendations_log/#33 и Anomalies_Log/#34): Sheets-запись
"Write PhenoAge_Log" — СНЯТА, только Postgres. Ничего не читает
PhenoAge_Log из Sheets (card-service уже полностью на health.phenoage_log).
Заодно устраняет реальный источник хрупкости: у Write PhenoAge_Log была
составная matchingColumns=[date, formula_version] — расширять
sheets_client.append_or_update_row() под составной ключ ради одной таблицы,
которую никто не читает, не по бюджету сложности. Если понадобится обратно
— несложно восстановить, скажи.

НАЙДЕНО, тот же класс бага, что уже был у Collect_Biohacking_Data (#34):
"Write PA PG" реально падал в проде 2026-09-12 с идентичной ошибкой n8n
Postgres-ноды ("Query Parameters must be a string...") — структурно тот же
баг n8n-ноды на динамическом queryReplacement, не в самой логике. Порт на
psycopg устраняет этот класс сбоя как побочный эффект."""
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VL = timezone(timedelta(hours=10))
WEEKLY_HOUR_VL = 9
HEALTH_DB_SHEET_ID = "1M8focgZBHCbhLEQb4GoyxTYxedA-FcdjQ_XakX5SG2w"

_REF_DEFAULT = {"alb": 47, "creat": 75, "gluc": 4.6, "crp": 0.5, "lymph": 33, "mcv": 88, "rdw": 12.5, "alp": 60, "wbc": 5}

PHENO_MARKER_DEFS = {
    "alb": {"ids": ["M008"], "rx": re.compile(r"альбумин", re.I)},
    "creat": {"ids": ["M004"], "rx": re.compile(r"креатинин", re.I)},
    "gluc": {"ids": ["M003"], "rx": re.compile(r"глюкоза", re.I)},
    "crp": {"ids": ["M024"], "rx": re.compile(r"с-реактивн|срб|crp", re.I)},
    "lymph": {"ids": ["M062"], "rx": re.compile(r"лимфоциты\s*%", re.I)},
    "mcv": {"ids": ["M043"], "rx": re.compile(r"mcv|средний объ[её]м эритроцит", re.I)},
    "rdw": {"ids": ["M049"], "rx": re.compile(r"rdw|ширина распред.*эритроцит", re.I)},
    "alp": {"ids": ["M017"], "rx": re.compile(r"щелочн(ая)? фосфатаз|alp|щф", re.I)},
    "wbc": {"ids": ["M039"], "rx": re.compile(r"лейкоциты|wbc", re.I)},
}
KEYS = list(PHENO_MARKER_DEFS.keys())
RARE = KEYS  # порт RARE из оригинала — буквально те же 9 ключей


def _num(v):
    if v is None or v == "":
        return None
    s = re.sub(r"\s", "", str(v)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _d10(v) -> str:
    s = str(v if v is not None else "").strip()
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return s[:10]


def load_ref_config(cfg_rows: list[list]) -> dict:
    """Порт REF/FORMULA_VERSION из "Compute PhenoAge" — `cfg_rows` включает
    шапку первой строкой (сырой get_values, как из sheets_client)."""
    if not cfg_rows or len(cfg_rows) < 2:
        return _REF_DEFAULT, "Levine2018 / CRP=mg/L / 2026-09"
    header = cfg_rows[0]
    idx = {h: i for i, h in enumerate(header)}
    if "fkey" not in idx:
        return _REF_DEFAULT, "Levine2018 / CRP=mg/L / 2026-09"

    def g(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    seen = set()
    cfg = []
    for r in cfg_rows[1:]:
        k = g(r, "fkey")
        if not k or k in seen:
            continue
        seen.add(k)
        cfg.append(r)

    formula_version = "Levine2018 / CRP=mg/L / 2026-09"
    for r in cfg:
        if g(r, "fkey") == "crp_unit" and g(r, "note"):
            formula_version = g(r, "note")
            break

    ref = {}
    for r in cfg:
        if g(r, "row_type") == "marker":
            fkey = g(r, "fkey")
            v = _num(g(r, "ref_healthy"))
            if fkey and v is not None:
                ref[fkey] = v
    if not all(k in ref for k in _REF_DEFAULT):
        ref = _REF_DEFAULT
    return ref, formula_version


def key_for_marker_id(marker_id: str, id_to_name: dict) -> Optional[str]:
    for k, d in PHENO_MARKER_DEFS.items():
        if marker_id in d["ids"]:
            return k
        if d["rx"].search(id_to_name.get(marker_id) or ""):
            return k
    return None


def phenoage(x: dict) -> float:
    """Порт phenoAge() — формула Levine 2018."""
    ln_crp = math.log(max(x["crp"], 1e-4))
    xb = (-19.907 - 0.0336 * x["alb"] + 0.0095 * x["creat"] + 0.1953 * x["gluc"] + 0.0954 * ln_crp
          - 0.0120 * x["lymph"] + 0.0268 * x["mcv"] + 0.3306 * x["rdw"] + 0.00188 * x["alp"]
          + 0.0554 * x["wbc"] + 0.0804 * x["age"])
    m = 1 - math.exp(-1.51714 * math.exp(xb) / 0.0076927)
    pa = 141.50 + math.log(-0.00553 * math.log(1 - m)) / 0.09165
    return round(pa, 2)


def build_visit_map(results: list[dict], visits: list[dict], markers: list[dict]) -> dict:
    """Порт слияния Results+Visits+Markers -> visit_id -> {date, age, values}."""
    id_to_name = {m["Marker_ID"]: m.get("Name") or "" for m in markers}
    visit_map = {}
    for v in visits:
        visit_map[v["Visit_ID"]] = {"visit_id": v["Visit_ID"], "date": _d10(v.get("Date")), "age": _num(v.get("Age_at_Visit")), "values": {}}
    for r in results:
        vid = r.get("Visit_ID")
        k = key_for_marker_id(r.get("Marker_ID"), id_to_name)
        if not k:
            continue
        val = _num(r.get("Value"))
        if val is None:
            continue
        if vid not in visit_map:
            visit_map[vid] = {"visit_id": vid, "date": _d10(vid), "age": None, "values": {}}
        visit_map[vid]["values"][k] = val
    return visit_map


def compute_phenoage_result(results: list[dict], visits: list[dict], markers: list[dict],
                             ref: dict, formula_version: str, today: Optional[str] = None) -> dict:
    """Порт "Compute PhenoAge" (без Sheets-специфики) — возвращает
    {current, series, series_estimated, rows, missing_for_current}."""
    visit_map = build_visit_map(results, visits, markers)
    today = today or datetime.now(VL).date().isoformat()

    age_anchor = None
    for v in sorted((x for x in visit_map.values() if x["date"] and x["age"] is not None), key=lambda x: x["date"]):
        age_anchor = {"date": v["date"], "age": v["age"]}

    def age_at_date(date_str: str) -> Optional[float]:
        if age_anchor and date_str:
            dd = (datetime.fromisoformat(date_str) - datetime.fromisoformat(age_anchor["date"])).days / 365.25
            return round((age_anchor["age"] + dd) * 10) / 10
        try:
            y = int((date_str or "")[:4])
        except ValueError:
            return None
        return y - 1982

    def age_for(v: dict) -> Optional[float]:
        return v["age"] if v["age"] is not None else age_at_date(v["date"])

    sorted_visits = sorted((v for v in visit_map.values() if v["date"]), key=lambda v: v["date"])

    series = []
    for v in sorted_visits:
        age = age_for(v)
        have = [k for k in KEYS if v["values"].get(k) is not None]
        if len(have) == 9 and age is not None:
            pa = phenoage({**v["values"], "age": age})
            series.append({"date": v["date"], "chrono": age, "pheno": pa, "delta": round((pa - age) * 100) / 100})

    latest_val, latest_date = {}, {}
    for v in sorted_visits:
        for k in KEYS:
            if v["values"].get(k) is not None:
                latest_val[k] = v["values"][k]
                latest_date[k] = v["date"]
    have_latest = [k for k in KEYS if latest_val.get(k) is not None]
    missing_latest = [k for k in KEYS if latest_val.get(k) is None]
    cur_age = age_at_date(today)

    rows = []
    if len(have_latest) == 9:
        pa = phenoage({**latest_val, "age": cur_age})
        contributions = {}
        for k in KEYS:
            swapped = {**latest_val, "age": cur_age, k: ref[k]}
            contributions[k] = round((pa - phenoage(swapped)) * 100) / 100
        current = {
            "date": today, "chrono_age": round(cur_age * 10) / 10, "phenoage": pa,
            "delta": round((pa - cur_age) * 100) / 100,
            "markers_used": ", ".join(f"{k}:{latest_val[k]}@{latest_date[k]}" for k in have_latest),
            "marker_contributions": contributions,
            "marker_values": {k: {"value": latest_val.get(k), "measured": latest_date.get(k)} for k in KEYS},
            "oldest_marker_date": sorted(latest_date[k] for k in have_latest)[0],
            "missing": "", "formula_version": formula_version,
        }
        rows.append({
            "date": today, "chrono_age": current["chrono_age"], "phenoage": pa, "delta": current["delta"],
            "markers_used": current["markers_used"], "missing": "", "formula_version": formula_version,
            "contributions": json.dumps(contributions, ensure_ascii=False),
            "marker_values": json.dumps(current["marker_values"], ensure_ascii=False),
            "oldest_marker_date": current["oldest_marker_date"],
        })
    else:
        current = {"missing": ", ".join(missing_latest), "note": "не хватает маркеров для расчёта"}

    series_estimated = []
    for v in sorted_visits:
        age = age_for(v)
        if age is None:
            continue
        vals = dict(v["values"])
        carried = []
        for k in KEYS:
            if vals.get(k) is not None or k not in RARE:
                continue
            best = None
            for w in sorted_visits:
                if w["date"] <= v["date"] and w["values"].get(k) is not None:
                    best = w["values"][k]
            if best is not None:
                vals[k] = best
                carried.append(k)
        still_missing = [k for k in KEYS if vals.get(k) is None]
        measured_here = len([k for k in KEYS if v["values"].get(k) is not None])
        if not still_missing and measured_here >= 3:
            pa = phenoage({**vals, "age": age})
            series_estimated.append({"date": v["date"], "chrono": age, "pheno": pa,
                                      "delta": round((pa - age) * 100) / 100,
                                      "estimated": len(carried) > 0, "carried_forward": carried})

    for s in series:
        rows.append({"date": s["date"], "chrono_age": s["chrono"], "phenoage": s["pheno"], "delta": s["delta"],
                      "markers_used": "все 9 (визит)", "missing": "", "formula_version": formula_version})

    exact_dates = {s["date"] for s in series}
    for s in series_estimated:
        if s["date"] in exact_dates:
            continue
        label = "оценка: " + ("carry-forward " + "/".join(s["carried_forward"]) if s["carried_forward"] else "все 9")
        rows.append({"date": s["date"], "chrono_age": s["chrono"], "phenoage": s["pheno"], "delta": s["delta"],
                      "markers_used": label, "missing": "", "formula_version": formula_version})

    return {"current": current, "series": series, "series_estimated": series_estimated,
            "rows": rows, "missing_for_current": missing_latest}


_PA_KNOWN = ["date", "chrono_age", "phenoage", "delta", "markers_used", "missing",
             "formula_version", "contributions", "marker_values", "oldest_marker_date"]
_PA_PK = ["date", "formula_version"]


def write_phenoage_row(cur, row: dict) -> bool:
    """Порт "Build PA PG"+"Write PA PG" — динамический upsert по (date,
    formula_version). Возвращает False (не пишет) если PK-поля пусты, как в
    оригинале."""
    if any(row.get(k) in (None, "") for k in _PA_PK):
        return False
    keys = [k for k in _PA_KNOWN if row.get(k) not in (None, "")]
    for k in _PA_PK:
        if k not in keys:
            keys.insert(0, k)
    qi = lambda s: '"' + s.replace('"', '""') + '"'
    placeholders = ", ".join("%s" for _ in keys)
    set_clause = ", ".join(f"{qi(k)}=EXCLUDED.{qi(k)}" for k in keys if k not in _PA_PK)
    set_clause = (set_clause + ", " if set_clause else "") + "_synced_at=now()"
    query = (f"INSERT INTO health.phenoage_log ({', '.join(qi(k) for k in keys)}) VALUES ({placeholders}) "
             f"ON CONFLICT ({', '.join(qi(k) for k in _PA_PK)}) DO UPDATE SET {set_clause}")
    cur.execute(query, [str(row[k]) for k in keys])
    return True


def run_once() -> None:
    from app.db import get_conn
    from app.sheets_client import get_values

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Visit_ID", "Marker_ID", "Value", "Original_Unit", "Lab_Min", "Lab_Max" FROM health.results')
        cols = [c.name for c in cur.description]
        results = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute('SELECT "Marker_ID", "Name", "Category", "Standard_Unit" FROM health.markers')
        cols = [c.name for c in cur.description]
        markers = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute('SELECT "Visit_ID", "Date", "Age_at_Visit" FROM health.visits')
        cols = [c.name for c in cur.description]
        visits = [dict(zip(cols, r)) for r in cur.fetchall()]

    try:
        cfg_rows = get_values(HEALTH_DB_SHEET_ID, "PhenoAge_Config")
    except Exception:
        logger.exception("phenoage_calc: PhenoAge_Config недоступен, использую хардкод-эталоны")
        cfg_rows = []
    ref, formula_version = load_ref_config(cfg_rows)

    result = compute_phenoage_result(results, visits, markers, ref, formula_version)

    written = 0
    with get_conn() as conn, conn.cursor() as cur:
        for row in result["rows"]:
            if write_phenoage_row(cur, row):
                written += 1
        conn.commit()

    logger.info("phenoage_calc: посчитано %d строк истории, записано %d (current: %s)",
                len(result["rows"]), written,
                result["current"].get("phenoage") if "phenoage" in result["current"] else result["current"].get("note"))


def _sleep_until(hour: int, weekday: int) -> None:
    now = datetime.now(VL)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days_ahead = (weekday - nxt.weekday()) % 7
    nxt += timedelta(days=days_ahead)
    if nxt <= now:
        nxt += timedelta(days=7)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def run_scheduler() -> None:
    logger.info("phenoage_calc scheduler: старт (вс %02d:00 ВЛ)", WEEKLY_HOUR_VL)
    while True:
        try:
            _sleep_until(WEEKLY_HOUR_VL, weekday=6)
            run_once()
        except Exception:
            logger.exception("phenoage_calc: run_once упал — повтор через неделю")
            time.sleep(3600)
