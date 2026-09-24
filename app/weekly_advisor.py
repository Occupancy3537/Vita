"""Порт n8n `Weekly AI Advisor` (2026-09-20, группа 2, последний и самый
крупный порт этой волны — 26 нод n8n, ~760 строк JS суммарно: Build Context
450, AI Advisor (промпт) 65, Parse & Row 191, Sync Actions to Card 51).

Раз в неделю (воскресенье 20:00 ВЛ) собирает единый контекст пациента
(велнес, питание, лабы, симптомы, расследования, анамнез, медкарта,
картотека препаратов, профиль, прошлые советы) -> LLM даёт 1-3 приоритетных
действия на неделю -> разбор фильтруется через гейт активных
противопоказаний (грыжа L5/S1) -> пишется в health.recommendations_log +
уходит в Telegram + каждое действие отдельно проходит G1-G6 ворота
card-service (propose_recommendation, тот же путь, что и раньше).

Все 15 источников данных оригинала уже были в Postgres к моменту переноса —
health.daily_trends/day_sum/meals/results/markers/visits/phenoage_log (Волны
A/B), nutrient_targets/doctor_notes/recommendations_log/patient_state (эта
сессия, переносы #24/#28), anamnesis/symptom_log/investigations (Фазы
доктора), meds (#31, Health Watchdog). Единственный Sheets-источник
оригинала без готового аналога — Correlations_Log — читать не нужно вообще:
движок корреляций структурно отключён (`correlations.disabled = true`,
коллективное решение «слепой перебор пар на n≈150 = генератор шума»),
Build Context просто подставляет заглушку, ничего из листа не использует.

health.recommendations_log теперь пишется НАПРЯМУЮ в Postgres, не в Sheets.
Апдейт по Date — не через _nat_key (уникальный индекс по Date+первые 40
символов текста, не годится для идемпотентного перезапуска в тот же день с
чуть другим текстом), а явный DELETE+INSERT в одной транзакции — тот же
эффект, что был у Sheets "appendOrUpdate" с matchingColumns=[Date]."""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app import llm_usage
from app.ai_models import DEFAULT_MODEL
from app.dashboard import _dkey, _num
from app.db import get_conn
from app import notify
from app.patient_gate import profile_hernia_active, profile_swim_allowed, load_gate
from app import run_log, timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
WEEKLY_HOUR_VL = 20  # воскресенье
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = DEFAULT_MODEL  # 2026-09-22: см. app/ai_models.py
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]

MIN_ABS_DELTA = {
    "VO2 Max": 2, "RHR ночной": 3, "HRV ночная": 6, "Оценка сна": 7,
    "Восстановление (BB)": 12, "Стресс дневной": 6, "Сон (чистый, мин)": 30,
    "Эффективность сна": 4, "Калории": 350, "Белок": 25, "Шаги": 3500,
}
KEY_RX = re.compile(r"мрт|кт |узи|диагноз|не в норме|не норм|операц|госпитал|аллерг|перелом|вывих|"
                     r"хроническ|экструз|грыж|коксартроз|модик|modic|глауком|радикулопат|протруз", re.I)
ROUTINE_NORMAL_RX = re.compile(r"^(\s*)(в норме|в пределах нормы|норма\b|все показатели в норме)", re.I)
LOAD_RX = re.compile(
    r"интенсив|интервал|hiit|бег|пробеж|прыж|присед|становая|штанг|турник|подтяг|отжим|планк|"
    r"скручиван|макгил|ротац|наклон|подним[а-яё]*\s+(?:тяж|вес)|подн(ять|имать)\s+(?:тяж|вес)|"
    r"тяж(?:есть|ести|[её]л)|спринт|силов|кроссфит|бадминтон|берпи|выпад|растяж|мобилити|йог|лфк|"
    r"упражнен|качат|тренаж|подтягив|скакалк|степ[- ]аэроб|ударн", re.I,
)
ALLOWED_METRICS = ["sleep_min", "sleep_score", "sleep_eff", "hrv", "rhr", "body_battery", "stress", "steps", "vo2max"]
ACTION_TYPES = ["load_high", "load_low", "walk", "swim", "diet", "supplement", "sleep", "stress", "medical", "other"]
MACRO_COLS = {"Calories", "Proteins", "Carbs", "Fats"}


def _r1(x):
    return None if x is None else round(x * 10) / 10


def _avg(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _rows(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# =====================================================================
# 1. Сбор сырых данных
# =====================================================================

def _fetch_all(cur) -> dict:
    cur.execute('SELECT d.*, to_char(d."Дата", \'YYYY-MM-DD\') AS "Дата" FROM health.daily_trends d ORDER BY d."Дата"')
    daily = _rows(cur)

    cur.execute('SELECT d.*, to_char(d."Date", \'YYYY-MM-DD\') AS "Date" FROM health.day_sum d ORDER BY d."Date"')
    day_sum = _rows(cur)

    tz = timeutil.person_tz_name()
    cur.execute(
        "SELECT to_char(\"Date\" AT TIME ZONE %s, 'YYYY-MM-DD\"T\"HH24:MI') AS \"Date\", "
        '"Meal_description", "Calories", "NOVA" FROM health.meals '
        "WHERE (\"Date\" AT TIME ZONE %s)::date >= (now() AT TIME ZONE %s)::date - 10 "
        'ORDER BY "Date"',
        (tz, tz, tz),
    )
    meals = _rows(cur)

    cur.execute('SELECT "Нутриент", "Колонка_в_Meals", "Единица", "Норма_RDA_AI", "Верхний_предел_UL", "Категория" FROM health.nutrient_targets')
    targets = _rows(cur)

    cur.execute('SELECT note_date AS "Date", category AS "Category", note AS "Note", trigger AS "Trigger", plan AS "Plan" FROM health.doctor_notes')
    notes = _rows(cur)

    cur.execute('SELECT * FROM health.user_profile LIMIT 1')
    profile_rows = _rows(cur)

    cur.execute('SELECT "Date", "Status", "Priority", "Based_On", "Recommendation_Text", "Period_Type" FROM health.recommendations_log')
    recs = _rows(cur)

    cur.execute('SELECT "Status", "Condition", "Stage", "Confirmed_Date", "Source", "Contra_Load", "Contra_Food", "Contra_Other", "Allowed", "Provokers", "Review_Due" FROM health.patient_state')
    pstate = _rows(cur)

    cur.execute('SELECT "Q_ID", "Category", "Question", "Status", "Answer" FROM health.anamnesis')
    anam = _rows(cur)

    cur.execute(
        'SELECT symptom_id AS "Symptom_ID", to_char(ts, \'YYYY-MM-DD\') AS "Date", symptom AS "Symptom", '
        'system AS "System", severity AS "Severity", status AS "Status", change AS "Change", '
        'domain AS "Domain", context AS "Context", hypothesis AS "Hypothesis", notes AS "Notes" '
        'FROM health.symptom_log ORDER BY ts'
    )
    sym = _rows(cur)

    cur.execute(
        'SELECT inv_id AS "Inv_ID", to_char(opened, \'YYYY-MM-DD\') AS "Opened", status AS "Status", '
        'trigger AS "Trigger", trigger_detail AS "Trigger_Detail", hypothesis AS "Hypothesis", '
        'questions_pending AS "Questions_Pending", labs_suggested AS "Labs_Suggested", '
        'referral AS "Referral", doctor_brief AS "Doctor_Brief" FROM health.investigations'
    )
    inv = _rows(cur)

    cur.execute('SELECT "Med_ID", "Name", "Class", "Dose", "Schedule", "Status", "Started", "Stopped", "Reason", "Prescribed_By" FROM health.meds')
    meds = _rows(cur)

    cur.execute('SELECT "Visit_ID", "Marker_ID", "Value", "Original_Unit", "Lab_Min", "Lab_Max" FROM health.results')
    lab_res = _rows(cur)

    cur.execute('SELECT "Marker_ID", "Name", "Category", "Standard_Unit" FROM health.markers')
    lab_mark = _rows(cur)

    cur.execute('SELECT "Visit_ID", "Date" FROM health.visits')
    lab_visit = _rows(cur)

    cur.execute('SELECT date, chrono_age, phenoage, delta, markers_used, formula_version, contributions FROM health.phenoage_log')
    pheno_log = _rows(cur)

    cur.execute("SELECT date::text AS date, raw_anomalies FROM health.anomaly_log")
    anomaly_log = _rows(cur)

    # Премортем (2026-09-20, задача "1,3,4,5,7", проблема #5 "gate6_priority
    # никогда не получал реальные флаги") — нужен для _bioage_driver_patterns/
    # _overdue_lab_tests в sync_actions_to_card, тот же запрос, что и в
    # Health Watchdog (#31).
    cur.execute('SELECT "Test", "Status", "Next_Due" FROM health.lab_plan')
    lab_plan = _rows(cur)

    return {
        "daily": daily, "day_sum": day_sum, "meals": meals, "targets": targets, "notes": notes,
        "profile": (profile_rows[0] if profile_rows else {}), "recs": recs, "pstate": pstate, "anam": anam,
        "sym": sym, "inv": inv, "meds": meds, "lab_res": lab_res, "lab_mark": lab_mark,
        "lab_visit": lab_visit, "pheno_log": pheno_log, "anomaly_log": anomaly_log, "lab_plan": lab_plan,
    }


# =====================================================================
# 2. Build Context
# =====================================================================

def build_context(src: dict) -> dict:
    daily, day_sum, meals = src["daily"], src["day_sum"], src["meals"]
    targets, notes, profile = src["targets"], src["notes"], src["profile"]
    recs, pstate, anam = src["recs"], src["pstate"], src["anam"]
    sym, inv, meds = src["sym"], src["inv"], src["meds"]
    lab_res, lab_mark, lab_visit, pheno_log = src["lab_res"], src["lab_mark"], src["lab_visit"], src["pheno_log"]

    daily_sorted = sorted((r for r in daily if r.get("Дата")), key=lambda r: _dkey(r["Дата"]))
    last_date = _dkey(daily_sorted[-1]["Дата"]) if daily_sorted else _dkey(timeutil.now_local())
    last_ms = datetime.fromisoformat(last_date + "T00:00:00+00:00")

    def days_ago(d):
        return (last_ms - timedelta(days=d)).date().isoformat()

    win7, win30, win60, win90 = days_ago(7), days_ago(30), days_ago(60), days_ago(90)
    today = _dkey(timeutil.now_local())

    def not_future(d):
        k = str(d or "")[:10]
        return bool(k) and k <= today

    # ---- 1. Аномалии ----
    anom_expanded = []
    for row in src.get("anomaly_log", []):
        d = _dkey(row.get("date"))
        if not d:
            continue
        try:
            raw = json.loads(row.get("raw_anomalies") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        if not isinstance(raw, list):
            raw = []
        for a in raw:
            min_d = MIN_ABS_DELTA.get(a.get("label"), 0)
            if a.get("value") is not None and a.get("baseline_mean") is not None and abs(a["value"] - a["baseline_mean"]) < min_d:
                continue
            anom_expanded.append({"date": d, **a})

    def group_anoms(since_date):
        rel = [a for a in anom_expanded if since_date < a["date"] <= last_date]
        by: dict = {}
        for a in rel:
            g = by.setdefault(a["metric"], {"label": a.get("label"), "count": 0, "strong": 0, "dir": a.get("direction"),
                                             "last_value": None, "baseline": a.get("baseline_mean"), "worsening": 0,
                                             "improving": 0, "days": []})
            g["count"] += 1
            if a.get("severity") == "strong":
                g["strong"] += 1
            if a.get("interpretation") == "ухудшение":
                g["worsening"] += 1
            if a.get("interpretation") == "улучшение":
                g["improving"] += 1
            g["last_value"] = a.get("value")
            g["days"].append(a["date"])
        lst = sorted(by.values(), key=lambda g: (g["strong"], g["count"]), reverse=True)
        for g in lst:
            g["days"] = g["days"][-6:]
        return lst

    anomalies_7d = group_anoms(win7)
    anomalies_30d = group_anoms(win30)

    # ---- 3. Wellness ----
    def slice_daily(from_excl, to_incl):
        return [r for r in daily_sorted if from_excl < _dkey(r["Дата"]) <= to_incl]

    def wellness_avg(rows):
        return {
            "n_days": len(rows),
            "sleep_min": _r1(_avg([_num(r.get("Чистый_сон_мин")) for r in rows])),
            "sleep_score": _r1(_avg([_num(r.get("Оценка_сна_балл")) for r in rows])),
            "hrv": _r1(_avg([_num(r.get("ВСР_ночная")) for r in rows])),
            "rhr": _r1(_avg([_num(r.get("Пульс_ночной_средний")) for r in rows])),
            "body_battery_recharge": _r1(_avg([_num(r.get("Восстановление_BodyBattery")) for r in rows])),
            "stress": _r1(_avg([_num(r.get("Стресс_дневной_средний")) for r in rows])),
            "steps": round(_avg([_num(r.get("Шаги_за_вчера")) for r in rows]) or 0),
        }

    last_row = daily_sorted[-1] if daily_sorted else {}
    acute, chronic = _num(last_row.get("Training_Acute_Load")), _num(last_row.get("Training_Chronic_Load"))
    acwr_g = _num(last_row.get("ACWR_Garmin"))
    wellness = {
        "week": wellness_avg(slice_daily(win7, last_date)),
        "prev_week": wellness_avg(slice_daily(days_ago(14), win7)),
        "last_30d": wellness_avg(slice_daily(win30, last_date)),
        "last_90d": wellness_avg(slice_daily(win90, last_date)),
        "training_status": last_row.get("Training_Status"),
        "acwr": acwr_g if acwr_g is not None else (_r1(acute / chronic) if (acute is not None and chronic) else None),
        "acwr_status": (str(last_row.get("ACWR_Status") or "").strip().upper() or None),
        "vo2max": _num(last_row.get("VO2_Max")),
    }
    week_daily = slice_daily(win7, last_date)

    # ---- 4. Таймлайн лекарств (Daily_Trends) ----
    def med_set(s):
        if not s:
            return []
        return sorted(x.strip() for x in re.split(r"[\n,;|]+", str(s)) if x.strip())

    med_days = [{"date": _dkey(r["Дата"]), "set": med_set(r.get("Лекарства_принимаемые"))}
                for r in daily_sorted if _dkey(r["Дата"]) > win90]
    med_changes = []
    prev_key = None
    for d in med_days:
        cur_key = " | ".join(d["set"])
        if prev_key is not None and cur_key != prev_key:
            prev_arr = prev_key.split(" | ") if prev_key else []
            started = [m for m in d["set"] if m not in prev_arr]
            stopped = [m for m in prev_arr if m not in d["set"]]
            med_changes.append({"date": d["date"], "started": started, "stopped": stopped})
        prev_key = cur_key
    last_active, last_active_date = None, None
    for d in med_days:
        if d["set"]:
            last_active, last_active_date = d["set"], d["date"]
    cur_set = med_days[-1]["set"] if med_days else []

    def d10m(v):
        s = str(v if v is not None else "").strip()[:10]
        return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None

    meds_card = [
        {"name": str(m.get("Name") or m.get("Med_ID")).strip(), "class": (str(m.get("Class") or "").strip() or None),
         "dose": (str(m.get("Dose") or "").strip() or None), "schedule": (str(m.get("Schedule") or "").strip() or None),
         "status": (str(m.get("Status") or "").strip().lower() or "unknown"),
         "started": d10m(m.get("Started")), "stopped": d10m(m.get("Stopped")),
         "reason": (str(m.get("Reason") or "").strip() or None),
         "prescribed_by": (str(m.get("Prescribed_By") or "").strip() or None)}
        for m in meds if m and (m.get("Name") or m.get("Med_ID")) and str(m.get("Med_ID") or "").lower() != "demo-format"
    ]
    meds_active = [m for m in meds_card if m["status"] in ("active", "prn", "paused")]
    meds_stopped_recent = sorted(
        (m for m in meds_card if m["status"] == "stopped" and m["stopped"]
         and (datetime.fromisoformat(last_date) - datetime.fromisoformat(m["stopped"])).days <= 90),
        key=lambda m: m["stopped"], reverse=True,
    )
    medications = {
        "card_active": meds_active, "card_stopped_recent": meds_stopped_recent, "daily_log_current": cur_set,
        "changes_last_90d": med_changes[-8:],
        "status": (
            ("по картотеке Meds: " + "; ".join(
                m["name"] + (f" {m['dose']}" if m["dose"] else "") + (f" — {m['reason']}" if m["reason"] else "")
                for m in meds_active)
             + (f" | в дневнике Daily_Trends: {', '.join(cur_set)}" if cur_set
                else " | в дневнике Daily_Trends за последние дни не отмечено"))
            if meds_active else
            (f"по дневнику: {', '.join(cur_set)} (картотека Meds пуста)" if cur_set
             else (f"препаратов нет; последний активный курс был по {last_active_date} ({', '.join(last_active or [])})"
                   if last_active_date else "нет данных"))
        ),
    }

    # ---- 5. Медкарта (Doctor_Notes) ----
    recent_cutoff = days_ago(270)
    all_notes = sorted(
        (
            {"date": str(r.get("Date") or "")[:10].replace(".", "-"), "date_raw": str(r.get("Date") or "")[:10],
             "category": r.get("Category") or "", "note": str(r.get("Note") or ""),
             "trigger": (str(r.get("Trigger") or "")[:200] or None), "plan": (str(r.get("Plan") or "")[:250] or None)}
            for r in notes if r.get("Date") or r.get("Note")
        ),
        key=lambda n: n["date"], reverse=True,
    )
    all_notes = [n for n in all_notes if not_future(n["date"])]
    recent_notes = [
        {"date": n["date_raw"], "category": n["category"], "note": n["note"][:500], "trigger": n["trigger"], "plan": n["plan"]}
        for n in all_notes if n["date"] >= recent_cutoff
    ][:15]
    key_history = [
        {"date": n["date_raw"], "category": n["category"], "note": n["note"][:260]}
        for n in all_notes if n["date"] < recent_cutoff
        and KEY_RX.search(n["category"] + " " + n["note"])
        and not ("лаборат" in n["category"].lower() and ROUTINE_NORMAL_RX.search(n["note"]))
    ][:12]

    # ---- 6. Профиль ----
    profile_out = {}
    for k, v in profile.items():
        if k in ("row_number", "Telegram_ID", "", "User_ID"):
            continue
        s = str(v if v is not None else "").strip()
        if s:
            profile_out[k] = (s[:1200] + "…") if len(s) > 1200 else s

    # ---- 7. Прошлые рекомендации ----
    past_recs = [
        {"date": str(r["Date"])[:10], "priority": r.get("Priority"), "based_on": r.get("Based_On"),
         "text": str(r.get("Recommendation_Text") or "")[:900]}
        for r in sorted((r for r in recs if r.get("Date") and str(r.get("Status") or "") != "init"),
                         key=lambda r: str(r["Date"]), reverse=True)[:4]
    ]

    # ---- 8. Питание за неделю vs цели ----
    day_sum_sorted = sorted((r for r in day_sum if r.get("Date")), key=lambda r: _dkey(r["Date"]))
    week_nut = [r for r in day_sum_sorted if win7 < _dkey(r["Date"]) <= last_date]
    macro_avg = {
        "calories": round(_avg([_num(r.get("Calories")) for r in week_nut]) or 0),
        "protein_g": _r1(_avg([_num(r.get("Proteins")) for r in week_nut])),
        "carbs_g": _r1(_avg([_num(r.get("Carbs")) for r in week_nut])),
        "fats_g": _r1(_avg([_num(r.get("Fats")) for r in week_nut])),
    }
    deficits, excesses = [], []
    for t in targets:
        col = t.get("Колонка_в_Meals")
        if not col:
            continue
        rda, ul = _num(t.get("Норма_RDA_AI")), _num(t.get("Верхний_предел_UL"))
        cats = [s.strip() for s in str(t.get("Категория") or "").split(";")]
        is_limit = "Риск избытка" in cats
        wk = _avg([_num(r.get(col)) for r in week_nut])
        if wk is None:
            continue
        if is_limit and ul:
            pct = round((wk / ul) * 100)
            if pct > 100:
                excesses.append({"nutrient": t["Нутриент"], "avg_per_day": _r1(wk), "limit": ul, "unit": t.get("Единица"), "pct_of_limit": pct})
        elif rda:
            pct = round((wk / rda) * 100)
            if pct < 85:
                deficits.append({"nutrient": t["Нутриент"], "avg_per_day": _r1(wk), "target": rda, "unit": t.get("Единица"),
                                  "pct_of_target": pct, "category": ", ".join(cats)})
    deficits.sort(key=lambda d: d["pct_of_target"])
    excesses.sort(key=lambda e: e["pct_of_limit"], reverse=True)

    # ---- 8a. Меню за неделю ----
    meals_sorted = sorted((m for m in meals if m.get("Date")), key=lambda m: _dkey(m["Date"]))
    week_meals_rows = [m for m in meals_sorted if win7 < _dkey(m["Date"]) <= last_date]
    meals_by_day: dict = {}
    for m in week_meals_rows:
        day = _dkey(m["Date"])
        meals_by_day.setdefault(day, []).append({
            "time": str(m["Date"])[11:16], "dish": str(m.get("Meal_description") or "").strip()[:200],
            "kcal": round(_num(m.get("Calories")) or 0), "nova": m.get("NOVA") or None,
        })
    recent_meals = [{"date": d, "meals": meals_by_day[d]} for d in sorted(meals_by_day.keys())]

    # ---- 8b. Лаборатория ----
    mark_by_id = {m["Marker_ID"]: {"name": m.get("Name") or "", "unit": m.get("Standard_Unit") or "", "cat": m.get("Category") or ""}
                  for m in lab_mark}

    def pd_raw(s):
        s = str(s or "").strip()
        m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
        if m:
            return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        return m.group(0) if m else ""

    visit_date = {v["Visit_ID"]: pd_raw(v.get("Date")) for v in lab_visit if v.get("Visit_ID")}

    def pd(vid):
        return visit_date.get(vid) or pd_raw(vid)

    lab_latest: dict = {}
    for r in lab_res:
        mid = r.get("Marker_ID")
        if not mid:
            continue
        val = _num(r.get("Value"))
        if val is None:
            continue
        dt = pd(r.get("Visit_ID"))
        if mid not in lab_latest or dt > lab_latest[mid]["date"]:
            lab_latest[mid] = {"value": val, "date": dt, "lab_min": _num(r.get("Lab_Min")), "lab_max": _num(r.get("Lab_Max"))}

    labs_out_of_range, labs_recent = [], []
    year_ago = days_ago(365)
    for mid, x in lab_latest.items():
        nm = mark_by_id.get(mid, {}).get("name") or mid
        flag = ("ниже нормы" if (x["lab_min"] is not None and x["value"] < x["lab_min"])
                else "выше нормы" if (x["lab_max"] is not None and x["value"] > x["lab_max"]) else None)
        rec = {"marker": nm, "value": x["value"], "unit": mark_by_id.get(mid, {}).get("unit"),
               "ref": f"{x['lab_min'] if x['lab_min'] is not None else ''}–{x['lab_max'] if x['lab_max'] is not None else ''}",
               "date": x["date"], "flag": flag}
        if flag:
            labs_out_of_range.append(rec)
        if x["date"] >= year_ago:
            labs_recent.append(rec)
    labs_out_of_range.sort(key=lambda r: r["date"], reverse=True)
    labs_recent.sort(key=lambda r: r["date"], reverse=True)

    def marker_history(name_rx):
        rows = sorted(
            ({"date": pd(r.get("Visit_ID")), "value": _num(r.get("Value"))} for r in lab_res
             if name_rx.search(mark_by_id.get(r.get("Marker_ID"), {}).get("name") or "")),
            key=lambda r: r["date"] or "",
        )
        rows = [r for r in rows if r["date"] and r["value"] is not None]
        return rows[-8:]

    lab_trends = {"wbc": marker_history(re.compile(r"лейкоциты|wbc", re.I)),
                  "glucose": marker_history(re.compile(r"глюкоза", re.I))}

    phenoage_latest = None
    valid_pheno = sorted(
        (r for r in pheno_log if r.get("formula_version") and r["formula_version"] != "init" and _num(r.get("phenoage")) is not None),
        key=lambda r: str(r.get("date") or ""), reverse=True,
    )
    if valid_pheno:
        r = valid_pheno[0]
        phenoage_latest = {"date": pd_raw(r.get("date")) or str(r.get("date") or "")[:10], "phenoage": _num(r.get("phenoage")),
                            "chrono_age": _num(r.get("chrono_age")), "delta": _num(r.get("delta")), "markers_used": r.get("markers_used")}

    # ---- активные противопоказания ----
    p0 = profile
    prof_oda = str(p0.get("ОДА и неврология") or p0.get("ОДА и неврология ") or "")
    pstate_empty = len(pstate) == 0
    active_restrictions = [
        {"condition": x.get("Condition"), "stage": x.get("Stage"), "confirmed": x.get("Confirmed_Date"),
         "source": x.get("Source"), "contra_load": x.get("Contra_Load"), "contra_food": x.get("Contra_Food"),
         "contra_other": x.get("Contra_Other"), "allowed": x.get("Allowed"), "provokers": x.get("Provokers"),
         "review_due": x.get("Review_Due")}
        for x in pstate if str(x.get("Status") or "").lower() == "active"
    ]
    if not active_restrictions and profile_hernia_active(prof_oda):
        active_restrictions.append({
            "condition": ("Грыжа/радикулопатия (Patient_State не прочитан, профиль подтверждает)" if pstate_empty
                          else "Грыжа/радикулопатия (из профиля, Patient_State без активных ограничений)"),
            "contra_load": "осевая нагрузка, подъём тяжестей, скручивания, бег, прыжки, интервалы",
            "allowed": "ходьба, плавание" if profile_swim_allowed(prof_oda) else "ходьба",
            "source": "User_Profile", "degraded": pstate_empty or None,
        })
    # 2026-09-21 (AGENT_SYNC #38/#39): гейт-РЕШЕНИЕ (блокировать нагрузку да/нет)
    # берётся из единой app.patient_gate.load_gate() — той же функции, что и
    # dashboard.py. Раньше этот модуль считал его сам через active_restrictions
    # и расходился с dashboard на входе «активная запись без Contra_Load +
    # грыжа в профиле» (dashboard блокировал, советник — нет, см. разбор в
    # patient_gate.load_gate()). active_restrictions выше остаётся как есть —
    # это контекст для LLM (нужны и contra_food/contra_other, не только load),
    # а не источник самого решения о блокировке.
    gate = load_gate(pstate, profile)
    restrictions_unknown = bool(gate.get("degraded"))

    # ---- анамнез ----
    anam_answered = [a for a in anam if a.get("Q_ID") and str(a.get("Status")) == "answered" and str(a.get("Answer") or "").strip()]
    anam_by_cat: dict = {}
    for a in anam_answered:
        cat = str(a.get("Category") or "прочее").split("/")[0]
        anam_by_cat.setdefault(cat, []).append({"q": a.get("Question"), "a": str(a["Answer"]).strip()})
    anamnesis = {
        "progress": f"{len(anam_answered)}/{len([a for a in anam if a.get('Q_ID')])}",
        "by_category": anam_by_cat,
        "still_collecting": any(a.get("Q_ID") and str(a.get("Status")) in ("pending", "asked") for a in anam),
    }

    # ---- симптомы ----
    sym_valid = sorted(
        (
            {"id": s.get("Symptom_ID"), "date": _dkey(s.get("Date")), "symptom": s.get("Symptom"), "system": s.get("System") or "",
             "severity": _num(s.get("Severity")), "status": str(s.get("Status") or "").lower(), "change": s.get("Change") or "",
             "domain": s.get("Domain") or "", "context": s.get("Context") or "", "hypothesis": s.get("Hypothesis") or "",
             "note": s.get("Notes") or ""}
            for s in sym if s.get("Symptom") and str(s.get("Symptom_ID") or "").lower() != "demo-format"
        ),
        key=lambda s: s["date"], reverse=True,
    )
    symptoms = {
        "active": [s for s in sym_valid if s["status"] in ("active", "monitoring")][:15],
        "recently_changed": [s for s in sym_valid if re.search(r"усил|ослаб|появ|верн|прош", s["change"], re.I)][:10],
        "resolved_last_90d": [{"symptom": s["symptom"], "date": s["date"]} for s in sym_valid
                               if s["status"] == "resolved" and s["date"] >= _dkey(days_ago(90))],
        "total": len(sym_valid),
    }

    # ---- расследования ----
    investigations = sorted(
        (
            {"id": v.get("Inv_ID"), "opened": _dkey(v.get("Opened")), "status": str(v.get("Status") or "").lower(),
             "trigger": v.get("Trigger") or "", "detail": v.get("Trigger_Detail") or "", "hypothesis": v.get("Hypothesis") or "",
             "pending": v.get("Questions_Pending") or "", "labs": v.get("Labs_Suggested") or "", "referral": v.get("Referral") or "",
             "brief": v.get("Doctor_Brief") or ""}
            for v in inv if v.get("Inv_ID") and str(v.get("Inv_ID")).lower() != "demo-format" and str(v.get("Status") or "").lower() != "closed"
        ),
        key=lambda v: v["opened"], reverse=True,
    )

    correlations = {"computed": None, "disabled": True, "note": "движок корреляций отключён"}

    return {
        "today": today,
        "data_coverage": f"wellness по {last_date}, nutrition по {(_dkey(day_sum_sorted[-1]['Date']) if day_sum_sorted else '?')}",
        "patient_profile": profile_out,
        "anamnesis": anamnesis,
        "symptoms": symptoms,
        "investigations": investigations,
        "active_restrictions": active_restrictions,
        "restrictions_unknown": restrictions_unknown,
        "load_gate": gate,
        "medical_record_recent": recent_notes,
        "medical_history_key": key_history,
        "medications": medications,
        "past_recommendations": past_recs,
        "window": {"from": win7, "to": last_date, "days_with_wellness": len(week_daily), "days_with_nutrition": len(week_nut)},
        "anomalies_last_7d": anomalies_7d, "anomalies_last_30d": anomalies_30d,
        "correlations": correlations,
        "wellness": wellness,
        "nutrition": {"macros_avg_per_day": macro_avg, "deficits": deficits, "excesses": excesses, "recent_meals": recent_meals},
        "labs": {"phenoage": phenoage_latest, "out_of_range": labs_out_of_range, "recent": labs_recent[:20], "trends": lab_trends},
    }


# =====================================================================
# 3. Промпт + LLM
# =====================================================================

def build_prompt(ctx: dict) -> str:
    today = timeutil.now_local().strftime("%Y-%m-%d")
    ctx_json = json.dumps(ctx, ensure_ascii=False)
    return f"""СЕГОДНЯ: {today}. Все данные в контексте — это ПРОШЛОЕ (поле context.today и context.data_coverage). Категорически запрещено упоминать любые даты позже сегодняшней или описывать события, которые «случатся». Если в медкарте попалась дата из будущего — это ошибка ввода, игнорируй такую запись.

Ты — семейный врач Влада по превентивной медицине и активному долголетию. Ты ведёшь его не первый месяц: у тебя есть его медкарта, профиль по системам организма, история твоих прошлых рекомендаций и график приёма препаратов. Раз в неделю ты разбираешь свежие данные и даёшь 1–3 приоритетных действия на следующую неделю. Это ЭКРАН РЕШЕНИЙ, а не пересказ цифр: коротко, по делу, каждое действие — с обоснованием «почему именно это и именно сейчас» на его числах.

ПОЛНЫЙ КОНТЕКСТ (JSON):
{ctx_json}

Как читать разделы:
- patient_profile — постоянное досье по системам (нервная система/HRV, ОДА, ССС, аллергии, психопрофиль, телосложение). Ключевое: высокая чувствительность к кофе, стрессу и ФАРМПРЕПАРАТАМ.
- anamnesis — одноразовый сбор: наследственность (болезни и возраст событий у родителей и родни, ранние смерти), перенесённые болезни, операции, аллергии на лекарства и еду, образ жизни (курение, алкоголь), прививки, пройденные скрининги. by_category — ответы по разделам. Учитывай при оценке рисков и прежде чем советовать добавку/нагрузку/скрининг. still_collecting=true — сбор ещё идёт, разделов может не хватать, не делай выводов из отсутствия данных.
- symptoms — субъективные жалобы, записанные врачом-агентом структурно (боль, энергия, головные боли, ЖКТ, онемение…). active — что беспокоит сейчас; recently_changed — что появилось/усилилось/ослабло/прошло; resolved_last_90d — что ушло. Сопоставляй динамику симптома с изменениями в питании/активности/лекарствах в тот же период. Если симптом улучшился — попробуй объяснить чем; если появился новый или усилился — это сигнал наравне с аномалиями метрик.
- investigations — активные расследования врача-агента (жалоба/отклонение → гипотеза → сбор данных → выжимка для живого врача). Если по теме уже идёт расследование — не дублируй разбор, можешь сослаться («идёт разбор по X»). status=report_ready + Referral — значит выжимка для врача готова, пациенту пора записаться.
- medical_record_recent — записи медкарты за ~9 месяцев (симптомы, МРТ, смены терапии). medical_history_key — значимые более ранние находки. Сопоставляй текущие сигналы с историей: было ли такое раньше, что помогало.
- medications — card_active (картотека Meds: препарат, класс, доза, зачем, кто назначил), card_stopped_recent (курсы, законченные за 90 дн), daily_log_current (фактический приём по дневнику Daily_Trends), changes_last_90d (старты/стопы по дневнику). ВСЕГДА проверяй: не совпадает ли сдвиг метрики (HRV, сон, стресс) со стартом или окончанием курса — особенно смотри card_stopped_recent (пример: курс кончился → метрика возвращается к исходной). Если card_active и daily_log_current расходятся — скажи об этом одной фразой, попроси Влада свериться.
- past_recommendations — что ты советовал раньше. Если проблема та же и совет не выполнен — скажи об этом одной фразой, не переобъясняй. Фокус на новом.
- anomalies_last_7d / 30d — метрики, отклонявшиеся от его индивидуальной нормы. worsening = в плохую сторону, strong = сильно, count = дней. Подсказки «посмотри сюда», не диагнозы.
- correlations.disabled = true — движок корреляций отключён (слепой перебор пар на малых данных = шум). Не упоминай корреляции и «связи в данных». Гипотезы о причинах ищи через symptoms + nutrition + labs напрямую, вывод — «проверить элиминацией / у врача».
- wellness — week vs prev_week vs last_30d vs last_90d. Смотри и неделя-к-неделе, и на месячный/квартальный тренд. acwr — острая/хроническая нагрузка. acwr_status (LOW / OPTIMAL / HIGH) — вердикт Garmin, он приоритетнее числа: HIGH = риск перегруза (критично при грыже L5/S1), LOW = недобор нагрузки.
- nutrition.deficits / excesses — среднее за день против его целевых норм (в процентах).
- nutrition.recent_meals — реальные блюда за неделю по дням (время, название, ккал, NOVA где размечено: 1 необработанное … 4 ультра-обработанное). Говори о питании как нутрициолог: называй конкретные блюда и паттерны («сосиски третий день подряд», «мало овощей, много хлеба», «газировка почти каждый вечер»), а не только проценты нутриентов. Совет по питанию опирается на то, что он реально ел, а не только на % от нормы.
- labs — анализы крови. НЕ ПРОПУСКАТЬ. phenoage: расчётный биологический возраст и Δ к паспортному (может быть null). out_of_range: маркеры вне лабораторной нормы на последнем измерении. trends: динамика за годы — СМОТРИ НА ТРЕНД, а не на точку. Низкие годами лейкоциты — конституция, а не новый сигнал.
- active_restrictions — активные противопоказания. Это жёсткий запрет, не рекомендация. Если restrictions_unknown=true — карту пациента не удалось прочитать: считай, что ограничение по спине ДЕЙСТВУЕТ (только ходьба), нагрузочных действий не давай вообще.

ТВОЯ ЗАДАЧА:
0. ЖЁСТКОЕ ОГРАНИЧЕНИЕ. Для КАЖДОГО действия проверь active_restrictions: contra_load / contra_food / contra_other. Попадает — не давай это действие, замени на допустимое из поля allowed. Пример: грыжа L5/S1 → нельзя интервалы, интенсив, подъём тяжестей, скручивания, бег, прыжки; можно ходьбу и плавание. Это приоритетнее любых данных о восстановлении.
1. Отранжируй сигналы сам. Выбери 1–3 главных, остальное не перечисляй.
2. Для каждого — одно конкретное действие на неделю + 1–2 фразы «почему именно это сейчас» на его числах и истории («HRV 44 против обычных 50; Тиогамма закончилась 31.08 — вероятно возврат к исходному»).
3. Прежде чем советовать добавку/продукт/нагрузку — проверь: нет ли в медкарте, анализах или профиле причины этого не делать (аллергия, конфликт с препаратами, противопоказание).
4. Лаборатория — одной-двумя фразами: текущий PhenoAge и Δ к паспорту; есть ли СЕЙЧАС что-то НОВОЕ вне нормы (стабильные многолетние особенности не пересказывай). Если phenoage пустой — назови, каких маркеров не хватает и что добавить в следующую сдачу.
5. Спокойная неделя или мало данных — скажи прямо одной фразой, не выдумывай проблемы.

ФОРМАТ ОТВЕТА:
- Сначала — текст разбора. Максимум 1200 символов. Не влезаешь — режь обоснования, НЕ действия. Начни сразу с сути, без приветствия.
- Простой текст. Без Markdown (звёздочки, решётки, жирный). Абзацы и редкие эмодзи.
- Запрещены слова: z-score, z-скор, p-value, p-значение, стандартное отклонение, baseline, медиана. По-человечески.
- Не назначай препараты и дозы. Про грыжу — только режим. Про терапию — «обсуди с лечащим врачом», но не назначай.

МАШИННЫЙ БЛОК (обязателен, в самом конце, СРАЗУ после текста разбора, в лимит 1200 не входит):
<<<ACTIONS
[{{"title":"...","why":"...","expect":"...","priority":"высокий","metric":"sleep_min","direction":"up","magnitude":20,"type":"sleep"}}]
ACTIONS>>>
Правила блока:
- 1–3 объекта, ВСЕГДА минимум один. РОВНО те действия, что ты дал выше. Только действия, не статусы.
- Спокойная неделя без новых действий → один объект {{"title":"Держать текущий режим","why":"...","expect":"...","priority":"низкий","metric":null,"direction":null,"magnitude":null,"type":"other"}}.
- title — коротко, что делать. why — почему сейчас. expect — какой эффект ожидаешь.
- priority: высокий | средний | низкий.
- metric — ключ, по которому будет виден эффект, строго один из: sleep_min, sleep_score, sleep_eff, hrv, rhr, body_battery, stress, steps, vo2max. Если действие ими не измеряется — null.
- direction/magnitude — заполняются, ТОЛЬКО если metric не null: direction — "up" или "down" (в какую сторону должна сдвинуться метрика), magnitude — на сколько (число, в единицах самой метрики: минуты для sleep_min, мс для hrv, уд/мин для rhr, баллы для sleep_score/body_battery/vo2max, шаги для steps). Реалистичная величина за 7 дней, не оптимистичный максимум. Если metric null — оба null.
- type — ЧТО это за действие, СТРОГО одно из закрытого списка (по нему код проверяет противопоказания, не по тексту):
  load_high — интенсив, интервалы/HIIT, силовая, бег, прыжки, спринт, кроссфит, подъём тяжестей, любые ударные;
  load_low — лёгкая аэробика, растяжка, мобилити, йога, ЛФК, любые упражнения с движением корпуса/осевой нагрузкой;
  walk — только ходьба; swim — только плавание;
  diet — питание/нутриенты; supplement — добавка/препарат; sleep — режим/гигиена сна;
  stress — стресс/восстановление/дыхание/ментальное; medical — обследование, визит к врачу, анализ;
  other — всё, что не подходит (напр. «держать режим»).
  Если сомневаешься между load_low и другим — ставь load_low. При активном ограничении по спине код оставит только walk/swim/diet/supplement/sleep/stress/medical/other.
- check — ТОЛЬКО для type:"diet", и только если действие проверяется ОДНИМ числом по факту питания; иначе null (это нормально, не у каждого диет-совета есть числовой критерий). Формат:
  {{"kind":"limit"|"goal","key":"<точное "nutrient" как в nutrition.deficits/excesses, напр. "Натрий">","op":"<="|">=","value":число,"unit":"единица","days_of_7":число от 1 до 7}}
  kind:"limit" (снизить/не превышать) → op строго "<=". kind:"goal" (поднять/достичь) → op строго ">=".
  key — точное название нутриента из nutrition.deficits/excesses, либо Calories/Proteins/Carbs/Fats для калорийности/макросов.
  value — СЛЕДУЮЩИЙ РЕАЛИСТИЧНЫЙ ШАГ от текущего avg_per_day (deficits/excesses), а НЕ сразу целевая норма RDA/UL — если сейчас превышение в 2 раза, не проси лимит с понедельника.
  days_of_7 — сколько из ближайших 7 дней критерий должен выполняться, чтобы засчитать действие сделанным. Не всегда 7 — для первого шага реалистичнее 4-5 из 7.
- Валидный JSON в одну строку. После ACTIONS>>> не пиши ничего.
"""


def call_model(prompt: str, timeout: float = 60.0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return ""
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL, "temperature": 0.4, "max_tokens": 3000,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "reasoning": {"max_tokens": 800},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        llm_usage.record("weekly_advisor", MODEL, data.get("usage"))
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("weekly_advisor: вызов модели упал")
        return ""


# =====================================================================
# 4. Разбор ответа + строка для recommendations_log (порт "Parse & Row")
# =====================================================================

def _validate_check(a: dict, nutrient_to_col: dict, unknown_fields: list) -> Optional[dict]:
    if a.get("type") != "diet":
        return None
    c = a.get("check")
    if not isinstance(c, dict):
        return None
    kind = c.get("kind") if c.get("kind") in ("limit", "goal") else None
    key = c.get("key") if isinstance(c.get("key"), str) else None
    col = (key if key in MACRO_COLS else nutrient_to_col.get(key)) if key else None
    op_want = "<=" if kind == "limit" else (">=" if kind == "goal" else None)
    op = c.get("op") if c.get("op") == op_want else None
    value = c.get("value") if (isinstance(c.get("value"), (int, float)) and c["value"] > 0) else None
    days_of_7 = c.get("days_of_7") if (isinstance(c.get("days_of_7"), int) and 1 <= c["days_of_7"] <= 7) else None
    if not kind or not col or not op or value is None or days_of_7 is None:
        unknown_fields.append(f'check невалиден в "{str(a.get("title"))[:40]}": {json.dumps(c, ensure_ascii=False)[:200]}')
        return None
    return {"kind": kind, "key": key, "col": col, "op": op, "value": value,
            "unit": (str(c.get("unit"))[:20] if c.get("unit") else ""), "days_of_7": days_of_7}


def parse_advisor_response(raw_text: str, ctx: dict, targets: list[dict], prev_weekly: Optional[dict]) -> dict:
    """Порт "Parse & Row" (v4). `prev_weekly` — последняя ЧУЖАЯ (не сегодняшняя)
    запись recommendations_log с Period_Type='weekly', для проверки эскалации."""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("LLM вернул пустой ответ")

    m = re.search(r"<<<ACTIONS\s*([\s\S]*?)\s*ACTIONS>>>", text)
    block_missing = m is None
    parsed = []
    if m:
        text = re.sub(r"<<<ACTIONS\s*([\s\S]*?)\s*ACTIONS>>>", "", text).strip()
        try:
            j = json.loads(m.group(1).strip())
            parsed = j if isinstance(j, list) else (j.get("actions") if isinstance(j, dict) and isinstance(j.get("actions"), list) else [])
        except (TypeError, ValueError, json.JSONDecodeError):
            block_missing = True
    if not text:
        raise ValueError("LLM вернул пустой текст разбора (без машинного блока)")

    unknown_fields: list = []
    nutrient_to_col = {t["Нутриент"]: t["Колонка_в_Meals"] for t in targets if t.get("Нутриент") and t.get("Колонка_в_Meals")}

    actions = []
    for a in parsed:
        if not a or not a.get("title"):
            continue
        metric = a.get("metric") if a.get("metric") in ALLOWED_METRICS else None
        if a.get("metric") and not metric:
            unknown_fields.append(f'metric="{a["metric"]}" в "{str(a.get("title"))[:40]}"')
        a_type = a.get("type") if a.get("type") in ACTION_TYPES else "unknown"
        if a.get("type") and a_type == "unknown" and a["type"] != "unknown":
            unknown_fields.append(f'type="{a["type"]}" в "{str(a.get("title"))[:40]}"')
        entry = {
            "title": str(a["title"])[:120], "why": (str(a["why"])[:400] if a.get("why") else ""),
            "expect": (str(a["expect"])[:300] if a.get("expect") else ""),
            "priority": (a.get("priority") if a.get("priority") in ("высокий", "средний", "низкий") else "средний"),
            "metric": metric,
            "direction": (a.get("direction") if (metric and a.get("direction") in ("up", "down")) else None),
            "magnitude": (a.get("magnitude") if (metric and isinstance(a.get("magnitude"), (int, float))) else None),
            "type": a_type,
        }
        entry["check"] = _validate_check({**a, "type": a_type}, nutrient_to_col, unknown_fields)
        actions.append(entry)

    alerts = []
    if unknown_fields:
        alerts.append("⚠️ Советник использовал нераспознанный ключ: " + "; ".join(unknown_fields)
                       + " — поле обнулено, действие сохранено без него. Проверь промпт/список допустимых значений.")

    # ---- фильтр Patient_State ----
    # 2026-09-21 (AGENT_SYNC #38/#39): решение "блокировать нагрузку" берётся
    # из ctx["load_gate"] (единая app.patient_gate.load_gate(), общая с
    # dashboard.py) вместо независимого пересчёта из active_restrictions —
    # именно расхождение этого пересчёта с dashboard было найдено аудитом.
    gate = ctx.get("load_gate") or {}
    restr_unknown = bool(gate.get("degraded"))

    blocked = []
    if gate.get("blocked"):
        allowed_raw = str(gate.get("allowed") or "").lower()
        allow_walk = (not restr_unknown) and bool(re.search(r"ходьб|walk|прогул", allowed_raw))
        allow_swim = (not restr_unknown) and bool(re.search(r"плаван|бассейн|swim", allowed_raw))
        allowed_list = "ходьба" if restr_unknown else (gate.get("allowed") or "ходьба, плавание")
        kept = []
        for a in actions:
            t = a["type"]
            drop = False
            if t in ("load_high", "load_low"):
                drop = True
            elif t == "walk":
                drop = not allow_walk
            elif t == "swim":
                drop = not allow_swim
            elif LOAD_RX.search(f"{a['title']} {a['why']}"):
                drop = True
            elif restr_unknown and t in ("unknown", ""):
                drop = True
            if drop:
                blocked.append(a["title"])
            else:
                kept.append(a)
        actions = kept
        if restr_unknown:
            alerts.append("⚠️ Не удалось прочитать карту пациента (Patient_State/профиль). Ограничения по спине приняты как ДЕЙСТВУЮЩИЕ — нагрузочные действия вырезаны. Проверь советник.")
        elif blocked:
            text += ("\n\n⚠️ Отфильтровано (противоречит активному ограничению по спине): "
                     + "; ".join(blocked) + f". Разрешено: {allowed_list}.")

    actions = actions[:3]
    no_actions = len(actions) == 0
    if block_missing or no_actions:
        alerts.append("⚠️ Советник не сформулировал действия на эту неделю. Разбор ниже — посмотри глазами.")
    ok_actions = len(actions) > 0 and not block_missing and not no_actions

    # ---- эскалация: 2 разбора подряд без действий ----
    today_date = (ctx.get("window") or {}).get("to") or datetime.now(timezone.utc).date().isoformat()
    escalate = False
    if not ok_actions and prev_weekly and re.search(r"без_действий", str(prev_weekly.get("Status") or "")):
        escalate = True
    if escalate:
        alerts.insert(0, "🔴 ЭСКАЛАЦИЯ: советник ВТОРУЮ неделю подряд не может сформулировать действия. "
                          "Замкнутый цикл «совет → эффект» сломан — LLM не возвращает валидный машинный блок, либо контекст пустой. "
                          "Нужна ручная проверка советника (Build Context / промпт / модель).")

    based = []
    a7 = ctx.get("anomalies_last_7d") or []
    if a7:
        based.append("аномалии_7д: " + ", ".join(a["label"] for a in a7))
    corr = ctx.get("correlations") or {}
    if corr.get("priority"):
        based.append(f"корреляции: {len(corr['priority'])}")
    if (ctx.get("nutrition") or {}).get("deficits"):
        based.append("дефициты: " + ", ".join(d["nutrient"] for d in ctx["nutrition"]["deficits"]))
    if (ctx.get("nutrition") or {}).get("excesses"):
        based.append("избытки: " + ", ".join(e["nutrient"] for e in ctx["nutrition"]["excesses"]))
    if (ctx.get("wellness") or {}).get("acwr") is not None:
        based.append(f"ACWR {ctx['wellness']['acwr']}")
    if blocked:
        based.append(f"гейт: вырезано {len(blocked)}")
    if block_missing:
        based.append("!!! машинный блок не пришёл")
    elif no_actions:
        based.append("!!! действий 0")
    if escalate:
        based.append("!!! ЭСКАЛАЦИЯ 2х без_действий подряд")

    strong7 = sum(a.get("strong") or 0 for a in a7)
    priority = "высокий" if (escalate or strong7 >= 3) else ("средний" if strong7 >= 1 else "низкий")

    tail_obj = {"actions": actions, "issued": (ctx.get("window") or {}).get("to"), "blocked_by_gate": blocked}
    if block_missing:
        tail_obj["note"] = "советник не вернул машинный блок"
    elif no_actions:
        tail_obj["note"] = "советник вернул блок без действий"
    if escalate:
        tail_obj["escalation"] = True
    tail = "\n\n<<<ACTIONS\n" + json.dumps(tail_obj, ensure_ascii=False) + "\nACTIONS>>>"

    if blocked:
        alerts.append(f"⚠️ {len(blocked)} действие(й) вырезано фильтром Patient_State (противоречат ограничению по спине). Проверь, что осталось.")

    return {
        "Date": today_date, "Period_Type": "weekly", "Recommendation_Text": text + tail,
        "Telegram_Text": ("\n\n".join(alerts) + "\n\n━━━━━━━━━━\n\n" if alerts else "") + text,
        "Alert_Text": "\n\n".join(alerts), "Has_Alert": len(alerts) > 0,
        "Based_On": " | ".join(based), "Status": ("отправлено" if ok_actions else ("без_действий_эскалация" if escalate else "без_действий")),
        "Priority": priority, "actions": actions,
    }


def _prev_weekly(recs: list[dict], today_date: str) -> Optional[dict]:
    """Последняя ЧУЖАЯ (не сегодняшняя) запись recommendations_log с
    Period_Type='weekly' — нужна parse_advisor_response() для проверки
    эскалации (2 разбора подряд без действий). Порт условия из "Build
    Context"/n8n-выражения: Get Recommendations_Log -> filter Period_Type ==
    'weekly' && Date != сегодня -> sort по Date убыв. -> первая."""
    cand = [r for r in recs if r.get("Period_Type") == "weekly" and r.get("Date") and r["Date"] != today_date]
    if not cand:
        return None
    return sorted(cand, key=lambda r: r["Date"], reverse=True)[0]


# =====================================================================
# 4. Sync Actions to Card
# =====================================================================

PHENOAGE_DRIVER_THRESHOLD = 0.3  # |вклад маркера в дельту|, тот же порядок величины, что Z_MODERATE в anomaly_detector — "заметно", не "любой ненулевой"
LAB_OVERDUE_DAYS = 14  # тот же порог, что Health Watchdog (#31) уже использует для просроченного Lab_Plan

# Стеммированные варианты app.dashboard.PHENO_MARKERS — там regex настроен на
# точное совпадение для разбора клинических записей (Doctor_Notes), здесь же
# сопоставляем со свободным текстом действия от LLM, где слово может стоять в
# любом падеже ("глюкозу", "глюкозы") — точный "глюкоза" такое не поймает.
# Отдельная мини-карта, не полноценный стеммер — тот же стиль усечения корня,
# что уже в HERNIA_RX/LOAD_RX этого файла ("экструз", "радикулопат" и т.п.).
_PHENO_DRIVER_FREE_TEXT_RX = {
    "alb": re.compile(r"альбумин", re.I),
    "creat": re.compile(r"креатинин", re.I),
    "gluc": re.compile(r"глюкоз", re.I),
    "crp": re.compile(r"с-реактивн|срб\b|\bcrp\b", re.I),
    "lymph": re.compile(r"лимфоцит", re.I),
    "mcv": re.compile(r"\bmcv\b|средн\w* объ[её]м эритроцит", re.I),
    "rdw": re.compile(r"\brdw\b|ширин\w* распредел\w* эритроцит", re.I),
    "alp": re.compile(r"щелочн\w* фосфатаз|\balp\b|\bщф\b", re.I),
    "wbc": re.compile(r"лейкоцит|\bwbc\b", re.I),
}


def _bioage_driver_patterns(pheno_log: list[dict]) -> list:
    """Премортем (2026-09-20, проблема #5): раньше is_bioage_driver никогда не
    вычислялся, gate6_priority(False, False) всегда молчал. Берёт последнюю
    валидную запись health.phenoage_log, находит маркеры с contribution >=
    PHENOAGE_DRIVER_THRESHOLD В СТОРОНУ УХУДШЕНИЯ (положительный вклад —
    делает PhenoAge старше), возвращает их стеммированные regex."""
    valid = [r for r in pheno_log if r.get("formula_version") and r["formula_version"] != "init" and r.get("contributions")]
    if not valid:
        return []
    latest = sorted(valid, key=lambda r: str(r.get("date") or ""))[-1]
    contrib = latest.get("contributions")
    if isinstance(contrib, str):
        try:
            contrib = json.loads(contrib)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(contrib, dict):
        return []
    out = []
    for code, val in contrib.items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v >= PHENOAGE_DRIVER_THRESHOLD and code in _PHENO_DRIVER_FREE_TEXT_RX:
            out.append(_PHENO_DRIVER_FREE_TEXT_RX[code])
    return out


def _overdue_lab_tests(lab_plan: list[dict], today_date: str) -> list[str]:
    """Активные пункты Lab_Plan, просроченные на LAB_OVERDUE_DAYS+ — та же
    логика/порог, что уже в health_watchdog.py (#31), не придумываю новую."""
    out = []
    try:
        today = datetime.strptime(today_date, "%Y-%m-%d").date()
    except ValueError:
        return out
    for r in lab_plan:
        test = r.get("Test")
        if not test or str(r.get("Status") or "active").lower() in ("done", "paused", "archived"):
            continue
        due = str(r.get("Next_Due") or "")[:10]
        if len(due) != 10:
            continue
        try:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - due_date).days >= LAB_OVERDUE_DAYS:
            out.append(test)
    return out


_OVERDUE_MATCH_STOPWORDS = {"для", "или", "при", "как", "что", "это"}


def _action_bioage_flags(action: dict, driver_patterns: list, overdue_tests: list[str]) -> tuple[bool, bool]:
    """Best-effort сопоставление действия советника с драйверами био-возраста/
    просроченными анализами по ключевым словам в title+why — не гарантированная
    связка (советник — LLM, формулирует свободным текстом), но лучше, чем
    флаг, который никогда не срабатывает вообще. Порог длины слова — 3 (не 4):
    много названий анализов — короткие витаминные аббревиатуры (B12, D3, K2),
    4 отсекало бы их все; несколько частых коротких служебных слов исключены
    отдельно, чтобы не давать ложных совпадений на пустом месте."""
    text = f"{action.get('title') or ''} {action.get('why') or ''}"
    is_driver = any(rx.search(text) for rx in driver_patterns)
    text_low = text.lower()
    overdue = False
    for test in overdue_tests:
        words = [w for w in re.split(r"[^а-яёa-z0-9]+", test.lower())
                 if len(w) >= 3 and w not in _OVERDUE_MATCH_STOPWORDS]
        if any(w in text_low for w in words):
            overdue = True
            break
    return is_driver, overdue


def sync_actions_to_card(actions: list[dict], date: str, pheno_log: Optional[list[dict]] = None,
                          lab_plan: Optional[list[dict]] = None) -> str:
    """Порт "Sync Actions to Card" (51 строк JS). В оригинале — HTTP POST на
    card-service:8080/recommendations/propose для каждого действия отдельно;
    здесь это тот же самый процесс, поэтому вызываем propose_recommendation()
    напрямую (тот же путь G1-G6, никакого HTTP-обхода).

    ВКЛЮЧЕНО (2026-09-20, премортем, задача Влада "1,3,4,5,7", проблема #5):
    оригинальный JS НИКОГДА не передавал is_bioage_driver/metric_overdue в
    body — gate6_priority(False, False) всегда возвращал 'normal', ветка
    "приоритет высокий" в сводке не срабатывала ни разу. Теперь эти два флага
    реально считаются (_bioage_driver_patterns/_overdue_lab_tests/
    _action_bioage_flags) из phenoage_log.contributions и Lab_Plan — best-effort
    сопоставление по ключевым словам, не гарантированная связка, но реальная,
    а не всегда-False заглушка."""
    from app.recommendations import ProposeRequest, propose_recommendation

    driver_patterns = _bioage_driver_patterns(pheno_log or [])
    overdue_tests = _overdue_lab_tests(lab_plan or [], date)

    results = []
    for i, a in enumerate(actions):
        is_driver, overdue = _action_bioage_flags(a, driver_patterns, overdue_tests)
        req = ProposeRequest(
            title=a["title"], action=a["title"], rationale=(a.get("why") or None),
            kind=a.get("type"), source_ref=f"advisor:{date}:{i}", origin="advisor",
            started_ts=datetime.now(timezone.utc),
            is_bioage_driver=is_driver, metric_overdue=overdue,
        )
        if a.get("metric"):
            req.metric_key = a["metric"]
            req.metric_label = a["metric"]
            req.direction = a.get("direction")
            req.magnitude = a.get("magnitude")
            req.window_days = 7
            req.lag_days = 1
            req.baseline_days = 7
        try:
            resp = propose_recommendation(req)
            if resp.accepted:
                tail = ", приоритет высокий" if resp.priority == "high" else ""
                results.append(f"✅ {a['title']} ({resp.id}{tail})")
            else:
                results.append(f"⛔ {a['title']} — заблокировано ({resp.rejected_gate}: {resp.rejected_reason})")
        except Exception as e:
            logger.exception("weekly_advisor: propose_recommendation упал для %r", a["title"])
            results.append(f"⚠️ {a['title']} — card-service недоступен ({e})")

    return "\n".join(results)


# =====================================================================
# 5. Запись в health.recommendations_log + оркестрация + планировщик
# =====================================================================

def write_recommendations_log(cur, row: dict) -> None:
    """DELETE+INSERT по Date+Period_Type — тот же эффект, что у Sheets
    "appendOrUpdate" с matchingColumns=[Date], но без хрупкого генерируемого
    _nat_key (Date + первые 40 символов Recommendation_Text): при повторном
    прогоне в тот же день с чуть другим текстом _nat_key меняется и старая
    строка осталась бы висеть дублем — явный DELETE по Date+Period_Type этого
    не допускает."""
    cur.execute(
        'DELETE FROM health.recommendations_log WHERE "Date" = %s AND "Period_Type" = \'weekly\'',
        (row["Date"],),
    )
    cur.execute(
        'INSERT INTO health.recommendations_log '
        '("Date", "Period_Type", "Recommendation_Text", "Based_On", "Status", "Priority", '
        '"Telegram_Text", "Alert_Text", "Has_Alert") '
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            row["Date"], row["Period_Type"], row["Recommendation_Text"], row["Based_On"],
            row["Status"], row["Priority"], row["Telegram_Text"], row["Alert_Text"],
            "TRUE" if row["Has_Alert"] else "FALSE",
        ),
    )


def run_once() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        src = _fetch_all(cur)

    ctx = build_context(src)
    today_date = (ctx.get("window") or {}).get("to") or timeutil.today().isoformat()
    prev_weekly = _prev_weekly(src["recs"], today_date)

    raw = call_model(build_prompt(ctx))
    if not raw:
        logger.error("weekly_advisor: модель не ответила, разбор недели пропущен")
        notify.notify("weekly_advisor", "critical", "⚠️ Еженедельный разбор: модель не ответила, попробую в следующий раз.")
        return

    row = parse_advisor_response(raw, ctx, src["targets"], prev_weekly)

    summary = sync_actions_to_card(row["actions"], row["Date"], src["pheno_log"], src["lab_plan"])
    if summary:
        row["Telegram_Text"] = row["Telegram_Text"] + "\n\n🗂 card (тест, на текст выше не влияет):\n" + summary

    with get_conn() as conn, conn.cursor() as cur:
        write_recommendations_log(cur, row)
        conn.commit()

    notify.notify("weekly_advisor", "normal", f"🩺 Еженедельный разбор ({row['Date']})\n\n{row['Telegram_Text']}")
    logger.info("weekly_advisor: разбор недели %s готов, статус=%s", row["Date"], row["Status"])


def _sleep_until(hour: int, minute: int = 0, weekday: Optional[int] = None) -> None:
    """Фаза 3 (2026-09-22): сон до часа ПО ПОЯСУ ЧЕЛОВЕКА (timeutil), кусками
    по 10 минут — переключение /tz подхватывается без ожидания следующего дня."""
    timeutil.sleep_until_local(hour, minute, weekday=weekday)


def run_scheduler() -> None:
    logger.info("weekly_advisor scheduler: старт (вс %02d:00 ВЛ)", WEEKLY_HOUR_VL)
    while True:
        try:
            _sleep_until(WEEKLY_HOUR_VL, 0, weekday=6)  # 6 = воскресенье (Python Monday=0)
            run_once()
            run_log.mark_run("weekly_advisor")
        except Exception as e:
            logger.exception("weekly_advisor: run_once упал — повтор через неделю")
            alert_on_failure("weekly_advisor", e)
            time.sleep(3600)
