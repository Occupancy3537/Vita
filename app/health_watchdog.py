"""Порт n8n `Health Watchdog` (2026-09-20, группа 2/3). Ежедневная проверка:
(1) новая сдача крови → нужен LLM-разбор; (2) маркер вне референса, в т.ч.
«сдвиг к границе» ещё в пределах нормы; (3) резкое ухудшение симптома за
3 дня; (4) просроченный анализ по Lab_Plan; (5) тема молчит 10-365 дней,
не чаще одного нуджа в 7 дней; (6) HRV-паттерн Уэллтори (падение ≥20% от
30-дневной базы 2 дня подряд — санкция ZCode, Волна 2 18.09).

Дедуп раньше жил в $getWorkflowStaticData (`sd.nudged{}` + 2 скаляра) —
здесь в health.watchdog_nudged (произвольные ключи) + health.watchdog_state
(1-строчный singleton), переживает рестарт контейнера так же, как n8n
переживал рестарт.

НАЙДЕНО при переносе, не мой баг:
- Detect регулярно падает в проде (execution_entity: ~4 из последних 15
  дневных прогонов — task-runner disconnect) тем же классом, что и оба
  OOM-инцидента _System Check (#15/#20): узел читает health.results/markers/
  visits/day_sum/daily_trends ЦЕЛИКОМ, без фильтра по дате. Порт читает те
  же таблицы без урезания (алгоритму реально нужна вся история для трендов/
  визитов) — но в Python-процессе с готовым бюджетом памяти, не в отдельном
  раннере с диагностированным лимитом, поэтому этот конкретный класс сбоя
  не переносится автоматически (стоит понаблюдать после переезда).
- Переменная `dt` (срез Daily_Trends за период) использовалась в HRV-блоке
  ДО своего `const dt = periodRows(...)` на 30 строк ниже — в чистом V8 это
  ReferenceError (проверено отдельно), исполняется ли код в раннере n8n
  до этой строки вообще (или раньше падает на memory) - не выяснил
  однозначно. Порт считает `dt` ДО HRV-блока — очевидно так и задумывалось,
  не сохраняю порядок, из-за которого код мог быть мёртвым кодом.
- nutrition_period.avg_kcal в оригинале искал колонки 'Калории'/'Всего_ккал'
  — их нет в health.day_sum (колонка называется 'Calories', с миграции на
  Postgres переименование не подхватили) — в проде это поле ВСЕГДА было null
  в промпте LLM. Порт использует настоящее имя колонки."""
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.ai_models import DEFAULT_MODEL
from app.db import get_conn
from app.doctor import telegram
from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
CHECK_HOUR_VL = 9
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = DEFAULT_MODEL  # 2026-09-22: см. app/ai_models.py
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]

STALE_MIN_DAYS, STALE_MAX_DAYS, STALE_RENUDGE_DAYS = 10, 365, 7
CHRONIC_LEUKO_RX = re.compile(r"лейкоцит|wbc", re.I)
CHRONIC_LOW_RX = re.compile(r"лейкоцит|конституц|хроническ.*низк", re.I)
SPINE_RX = re.compile(r"поясниц|грыж|радикулопат|l5|s1", re.I)


def _num(v):
    if v is None or v == "":
        return None
    s = re.sub(r"\s", "", str(v).replace(",", "."))
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


def _days(a: str, b: str) -> float:
    return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds() / 86400


def _today_vl() -> str:
    return _d10((datetime.now(timezone.utc) + timedelta(hours=10)).date().isoformat())


def _fetch_sources(cur) -> dict:
    cur.execute('SELECT "Visit_ID", "Marker_ID", "Value", "Lab_Min", "Lab_Max", "Original_Unit" FROM health.results')
    results = [{"Visit_ID": a, "Marker_ID": b, "Value": c, "Lab_Min": d, "Lab_Max": e, "Original_Unit": f}
               for a, b, c, d, e, f in cur.fetchall()]

    cur.execute('SELECT "Marker_ID", "Name" FROM health.markers')
    markers = [{"Marker_ID": a, "Name": b} for a, b in cur.fetchall()]

    # "Date" в health.visits — TEXT (наследие Sheets, формат DD.MM.YYYY) — не
    # ::date, _d10() сам разбирает оба формата (тот же приём, что в bioage-порте).
    cur.execute('SELECT "Visit_ID", "Date" FROM health.visits')
    visits = [{"Visit_ID": a, "Date": b} for a, b in cur.fetchall()]

    cur.execute(
        "SELECT symptom_id, to_char(ts, 'YYYY-MM-DD'), symptom, severity, change, status "
        "FROM health.symptom_log ORDER BY ts"
    )
    sym_rows = [{"Symptom_ID": a, "Date": b, "Symptom": c, "Severity": d, "Change": e, "Status": f}
                for a, b, c, d, e, f in cur.fetchall()]

    cur.execute(
        "SELECT inv_id, status, to_char(updated, 'YYYY-MM-DD'), to_char(opened, 'YYYY-MM-DD') "
        "FROM health.investigations ORDER BY opened"
    )
    inv_rows = [{"Inv_ID": a, "Status": b, "Updated": c, "Opened": d} for a, b, c, d in cur.fetchall()]

    cur.execute(
        'SELECT to_char("Date", \'YYYY-MM-DD\'), "Добавленный сахар", "Насыщенные жиры", "Клетчатка", "Calories" '
        'FROM health.day_sum ORDER BY "Date"'
    )
    day_sum = [{"Date": a, "Добавленный сахар": b, "Насыщенные жиры": c, "Клетчатка": d, "Calories": e}
               for a, b, c, d, e in cur.fetchall()]

    cur.execute(
        'SELECT to_char("Дата", \'YYYY-MM-DD\'), "ВСР_ночная", "Пульс_ночной_средний", "Чистый_сон_мин", '
        '"Тренировка_Ккал", "Лекарства_принимаемые" FROM health.daily_trends ORDER BY "Дата"'
    )
    daily = [{"Дата": a, "ВСР_ночная": b, "Пульс_ночной_средний": c, "Чистый_сон_мин": d,
              "Тренировка_Ккал": e, "Лекарства_принимаемые": f} for a, b, c, d, e, f in cur.fetchall()]

    cur.execute('SELECT "Med_ID", "Name", "Class", "Dose", "Status", "Started", "Stopped", "Reason" FROM health.meds')
    meds_rows = [{"Med_ID": a, "Name": b, "Class": c, "Dose": d, "Status": e, "Started": f, "Stopped": g, "Reason": h}
                 for a, b, c, d, e, f, g, h in cur.fetchall()]

    cur.execute('SELECT "Status", "Contra_Other", "Note" FROM health.patient_state')
    pstate_rows = [{"Status": a, "Contra_Other": b, "Note": c} for a, b, c in cur.fetchall()]

    cur.execute('SELECT "Test", "Status", "Next_Due" FROM health.lab_plan')
    lab_plan = [{"Test": a, "Status": b, "Next_Due": c} for a, b, c in cur.fetchall()]

    return {
        "results": results, "markers": markers, "visits": visits, "sym_rows": sym_rows, "inv_rows": inv_rows,
        "day_sum": day_sum, "daily": daily, "meds_rows": meds_rows, "pstate_rows": pstate_rows, "lab_plan": lab_plan,
    }


def _load_state(cur):
    cur.execute("SELECT key, marked_at FROM health.watchdog_nudged")
    nudged = dict(cur.fetchall())
    cur.execute("SELECT last_reviewed_visit, last_read_fail_notice FROM health.watchdog_state WHERE id = 1")
    row = cur.fetchone()
    return nudged, (row[0] if row else None), (row[1] if row else None)


def _store_state(cur, nudge_updates: dict, last_reviewed_visit, last_read_fail_notice) -> None:
    for key, marked_at in nudge_updates.items():
        cur.execute(
            "INSERT INTO health.watchdog_nudged (key, marked_at) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET marked_at = EXCLUDED.marked_at",
            (key, marked_at),
        )
    if last_reviewed_visit is not None or last_read_fail_notice is not None:
        cur.execute(
            "INSERT INTO health.watchdog_state (id, last_reviewed_visit, last_read_fail_notice) "
            "VALUES (1, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "last_reviewed_visit = COALESCE(EXCLUDED.last_reviewed_visit, health.watchdog_state.last_reviewed_visit), "
            "last_read_fail_notice = COALESCE(EXCLUDED.last_read_fail_notice, health.watchdog_state.last_read_fail_notice)",
            (last_reviewed_visit, last_read_fail_notice),
        )


def detect(cur) -> dict:
    src = _fetch_sources(cur)
    results, markers, visits = src["results"], src["markers"], src["visits"]
    sym_rows, inv_rows = src["sym_rows"], src["inv_rows"]
    day_sum, daily, meds_rows, pstate_rows, lab_plan = (
        src["day_sum"], src["daily"], src["meds_rows"], src["pstate_rows"], src["lab_plan"])
    nudged, last_reviewed_visit, last_read_fail_notice = _load_state(cur)

    today = _today_vl()

    def d_ago(d):
        return _d10((datetime.now(timezone.utc) + timedelta(hours=10) - timedelta(days=d)).date().isoformat())

    mark_name = {m["Marker_ID"]: m["Name"] for m in markers if m.get("Marker_ID")}
    v_sorted = sorted((v for v in visits if v.get("Date") and v.get("Visit_ID")), key=lambda v: _d10(v["Date"]))
    newest = v_sorted[-1] if v_sorted else None
    prev = v_sorted[-2] if len(v_sorted) >= 2 else None
    newest_date = _d10(newest["Date"]) if newest else None

    def visit_markers(vid):
        o = {}
        for r in results:
            if r.get("Visit_ID") != vid:
                continue
            val = _num(r.get("Value"))
            if val is None:
                continue
            o[mark_name.get(r["Marker_ID"], r["Marker_ID"])] = {
                "value": val, "lo": _num(r.get("Lab_Min")), "hi": _num(r.get("Lab_Max")),
                "unit": r.get("Original_Unit") or "",
            }
        return o

    newest_m = visit_markers(newest["Visit_ID"]) if newest else {}
    prev_m = visit_markers(prev["Visit_ID"]) if prev else {}

    # --- (1) новая сдача ---
    visit_age_days = (
        (datetime.now(timezone.utc) - datetime.fromisoformat(newest_date + "T02:00:00+00:00")).total_seconds() / 86400
        if newest_date else 9999
    )
    already_reviewed = last_reviewed_visit == (newest["Visit_ID"] if newest else None)
    recent_enough = visit_age_days < 30
    needs_lab_review = bool(newest and not already_reviewed and recent_enough and len(newest_m) >= 2)

    # --- хронические маркеры из Patient_State — про них не нудить ---
    chronic_rx_text = " ".join(
        f"{p.get('Contra_Other') or ''} {p.get('Note') or ''}" for p in pstate_rows
    ).lower()

    def is_chronic_marker(name: str) -> bool:
        n = name.lower()
        return bool(CHRONIC_LEUKO_RX.search(n) and CHRONIC_LOW_RX.search(chronic_rx_text))

    # --- (2) маркеры вне референса — только из свежей сдачи (≤45 дн) ---
    abnormal = []
    if visit_age_days <= 45 and newest:
        for name, m in newest_m.items():
            if is_chronic_marker(name):
                continue
            base = {"key": f"m:{newest['Visit_ID']}:{name}", "name": name, "value": m["value"]}
            if m["lo"] is not None and m["value"] < m["lo"]:
                abnormal.append({**base, "ref": f"{m['lo']}–{m['hi'] if m['hi'] is not None else '?'}", "flag": "ниже нормы"})
            elif m["hi"] is not None and m["value"] > m["hi"]:
                abnormal.append({**base, "ref": f"{m['lo'] if m['lo'] is not None else '?'}–{m['hi']}", "flag": "выше нормы"})
            elif prev_m.get(name) and prev_m[name]["value"] is not None:
                pv = prev_m[name]["value"]
                if pv != 0 and abs(m["value"] - pv) / abs(pv) >= 0.18:
                    toward_hi = m["hi"] is not None and (m["value"] - pv) > 0 and (m["hi"] - m["value"]) < (m["hi"] - pv)
                    toward_lo = m["lo"] is not None and (m["value"] - pv) < 0 and (m["value"] - m["lo"]) < (pv - m["lo"])
                    if toward_hi or toward_lo:
                        abnormal.append({
                            **base, "ref": f"{m['lo'] if m['lo'] is not None else '?'}–{m['hi'] if m['hi'] is not None else '?'}",
                            "flag": f"сдвиг {pv} → {m['value']} (в норме, но к границе)",
                        })

    # --- (2c) просроченный Lab_Plan ---
    overdue_lab = []
    for r in lab_plan:
        if not r.get("Test") or str(r.get("Status") or "active").lower() in ("done", "paused", "archived"):
            continue
        due = _d10(r.get("Next_Due"))
        if not due or len(due) != 10:
            continue
        overdue_days = _days(today, due)
        if overdue_days >= 14:
            overdue_lab.append({"key": f"lp:{r['Test']}:{due}", "test": r["Test"], "due": due,
                                 "overdue_days": round(overdue_days)})

    # --- (3) резкое ухудшение симптома за 3 дня ---
    sym_alerts = []
    for s in sym_rows:
        if not s.get("Symptom") or str(s.get("Symptom_ID") or "").lower() == "demo-format":
            continue
        if _d10(s.get("Date")) < d_ago(3):
            continue
        severity = _num(s.get("Severity"))
        if not ((severity is not None and severity >= 4) or re.search(r"усил|верн|появ", str(s.get("Change") or ""), re.I)):
            continue
        sym_alerts.append({"key": f"s:{s['Symptom_ID']}:{_d10(s['Date'])}", "symptom": s["Symptom"],
                            "severity": severity, "change": s.get("Change") or "", "id": s["Symptom_ID"]})

    # --- открытое расследование? (TTL 30 дней) ---
    open_inv_rows = [v for v in inv_rows if v.get("Inv_ID") and str(v["Inv_ID"]).lower() != "demo-format"
                      and str(v.get("Status") or "").lower() == "open"]
    active_inv = [v for v in open_inv_rows if not _d10(v.get("Updated") or v.get("Opened"))
                  or _days(today, _d10(v.get("Updated") or v.get("Opened"))) < 30]
    open_investigation = len(active_inv) > 0
    stale_investigations = [v["Inv_ID"] for v in open_inv_rows if v not in active_inv]

    # --- (4) тема открыта и молчит N дней ---
    by_thread: dict = {}
    for s in sym_rows:
        sid = s.get("Symptom_ID")
        if not sid or str(sid).lower() == "demo-format":
            continue
        by_thread.setdefault(sid, []).append(s)
    stale_threads = []
    for sid, rows in by_thread.items():
        last = sorted(rows, key=lambda r: _d10(r["Date"]))[-1]
        status = str(last.get("Status") or "").lower()
        if status not in ("active", "monitoring"):
            continue
        last_date = _d10(last.get("Date"))
        if not last_date or len(last_date) != 10:
            continue
        age_days = round(_days(today, last_date))
        if age_days < STALE_MIN_DAYS or age_days > STALE_MAX_DAYS:
            continue
        key = f"stale:{sid}"
        days_since_nudge = _days(today, nudged[key]) if nudged.get(key) else 999
        if days_since_nudge < STALE_RENUDGE_DAYS:
            continue
        stale_threads.append({"key": key, "id": sid, "symptom": last["Symptom"], "status": status,
                               "age_days": age_days, "last_date": last_date})
    stale_threads.sort(key=lambda t: t["age_days"], reverse=True)
    stale_thread = stale_threads[0] if stale_threads else None

    # --- контекст периода (нужен и для HRV-блока, и для промпта — считаем ДО HRV,
    # оригинал использовал `dt` до его объявления, порт исправляет порядок) ---
    period_from = _d10(prev["Date"]) if prev else d_ago(120)

    def period_rows(rows, date_key):
        return [r for r in rows if period_from <= _d10(r.get(date_key)) <= (newest_date or today)]

    def avg_of(rows, key):
        vals = [_num(r.get(key)) for r in rows]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals) * 10) / 10 if vals else None

    ds = period_rows(day_sum, "Date")
    dt_rows = period_rows(daily, "Дата")

    # --- (6) HRV-паттерн Уэллтори ---
    abnormal_extra = []
    hrv_pts = sorted(
        ({"d": _d10(r.get("Дата")), "v": _num(r.get("ВСР_ночная"))} for r in daily),
        key=lambda x: x["d"],
    )
    hrv_pts = [x for x in hrv_pts if x["d"] and x["v"] is not None]
    if len(hrv_pts) >= 7:
        base_vals = [x["v"] for x in hrv_pts[:-2]]
        baseline = sum(base_vals) / len(base_vals)
        last2 = hrv_pts[-2:]
        if len(last2) == 2 and all(x["v"] < baseline * 0.80 for x in last2):
            key = f"hrv_drop:{last2[1]['d']}"
            if not nudged.get(key):
                abnormal_extra.append({
                    "key": key, "name": "HRV ниже базы (возможное начало заболевания)", "value": last2[1]["v"],
                    "ref": f"база ~{round(baseline)} мс, порог {round(baseline * 0.80)}",
                    "flag": f"ниже базы на {round((1 - last2[1]['v'] / baseline) * 100)}% (2 дня подряд)",
                    "symptom_hint": "Возможно начало ОРВИ или переутомления. Прими витамины, снизь нагрузку, выспись. Если появятся симптомы — напиши.",
                })
    abnormal.extend(abnormal_extra)

    new_abnormal = [a for a in abnormal if not nudged.get(a["key"])]
    new_sym_alerts = [a for a in sym_alerts if not nudged.get(a["key"])]
    new_overdue = [a for a in overdue_lab if not nudged.get(a["key"])]
    should_nudge = (not open_investigation) and bool(new_abnormal or new_sym_alerts or new_overdue or stale_thread)

    nutrition_period = {
        "days": len(ds),
        "avg_added_sugar_g": avg_of(ds, "Добавленный сахар"),
        "avg_sat_fat_g": avg_of(ds, "Насыщенные жиры"),
        "avg_fiber_g": avg_of(ds, "Клетчатка"),
        "avg_kcal": avg_of(ds, "Calories"),
    }
    meds_seen_raw = []
    for r in dt_rows:
        v = str(r.get("Лекарства_принимаемые") or "")
        meds_seen_raw.extend(x.strip() for x in v.split("\n") if x.strip())
    wellness_period = {
        "days": len(dt_rows),
        "avg_hrv": avg_of(dt_rows, "ВСР_ночная"),
        "avg_rhr": avg_of(dt_rows, "Пульс_ночной_средний"),
        "avg_sleep_min": avg_of(dt_rows, "Чистый_сон_мин"),
        "avg_training_kcal": avg_of(dt_rows, "Тренировка_Ккал"),
        "meds_seen": list(dict.fromkeys(meds_seen_raw))[:12],
    }

    # --- картотека препаратов ---
    meds_card = []
    for m in meds_rows:
        if not m or (not m.get("Name") and not m.get("Med_ID")) or str(m.get("Med_ID") or "").lower() == "demo-format":
            continue
        st, sp = _d10(m.get("Started")), _d10(m.get("Stopped"))
        meds_card.append({
            "name": str(m.get("Name") or m.get("Med_ID")).strip(), "class": (str(m.get("Class") or "").strip() or None),
            "dose": (str(m.get("Dose") or "").strip() or None), "status": (str(m.get("Status") or "").strip().lower() or "unknown"),
            "started": st if re.match(r"^\d{4}-\d{2}-\d{2}$", st or "") else None,
            "stopped": sp if re.match(r"^\d{4}-\d{2}-\d{2}$", sp or "") else None,
            "reason": (str(m.get("Reason") or "").strip() or None),
        })
    meds_active = [m for m in meds_card if m["status"] in ("active", "prn", "paused")]
    meds_changed_near_visit = []
    if newest_date:
        for m in meds_card:
            evt = m["stopped"] or m["started"]
            if evt and abs(_days(newest_date, evt)) <= 45:
                meds_changed_near_visit.append({**m, "event": f"курс завершён {m['stopped']}" if m["stopped"] else f"курс начат {m['started']}"})
    medications = {"active": meds_active, "changed_near_visit": meds_changed_near_visit}

    # --- (A4) листы, которые никогда легитимно не пустые ---
    read_failures = []
    if not daily:
        read_failures.append("Daily_Trends")
    if not visits:
        read_failures.append("Visits")
    if not results:
        read_failures.append("Results")
    if not pstate_rows:
        read_failures.append("Patient_State")
    if not sym_rows:
        read_failures.append("Symptom_Log")
    last_rf_days = _days(today, last_read_fail_notice) if last_read_fail_notice else 999
    notify_read_failure = bool(read_failures) and last_rf_days >= 3
    new_last_read_fail_notice = today if notify_read_failure else None

    return {
        "fire": needs_lab_review or should_nudge or notify_read_failure,
        "needs_lab_review": needs_lab_review, "should_nudge": should_nudge, "open_investigation": open_investigation,
        "read_failures": read_failures, "notify_read_failure": notify_read_failure,
        "newest_visit": ({"id": newest["Visit_ID"], "date": newest_date} if newest else None),
        "newest_markers": newest_m, "prev_visit_date": (_d10(prev["Date"]) if prev else None), "prev_markers": prev_m,
        "abnormal": abnormal, "new_abnormal": new_abnormal, "new_sym_alerts": new_sym_alerts, "new_overdue": new_overdue,
        "stale_thread": stale_thread, "stale_investigations": stale_investigations,
        "period": {"from": period_from, "to": newest_date or today},
        "nutrition_period": nutrition_period, "wellness_period": wellness_period, "medications": medications,
        "today": today, "_new_last_read_fail_notice": new_last_read_fail_notice,
    }


def build_prompt(d: dict) -> str:
    parts = [
        f"СЕГОДНЯ: {d['today']}. Ты формируешь короткое уведомление пациенту в Telegram от лица его "
        "превентивного врача. НЕ ставь диагноз — только наблюдения и гипотезы для обсуждения с живым врачом. "
        "Тон спокойный, без нагнетания."
    ]
    if d["needs_lab_review"]:
        nv = d["newest_visit"]
        parts.append(f"\n=== РАЗБОР СВЕЖЕЙ СДАЧИ КРОВИ ({nv['date']}, предыдущая {d['prev_visit_date'] or 'нет'}) ===")
        parts.append(f"Свежие маркеры: {json.dumps(d['newest_markers'], ensure_ascii=False)}")
        parts.append(f"Предыдущие маркеры: {json.dumps(d['prev_markers'], ensure_ascii=False)}")
        parts.append(
            f"Период между сдачами — питание: {json.dumps(d['nutrition_period'], ensure_ascii=False)}; "
            f"самочувствие/нагрузка: {json.dumps(d['wellness_period'], ensure_ascii=False)}"
        )
        med = d.get("medications") or {"active": [], "changed_near_visit": []}
        parts.append(
            "Препараты: сейчас по картотеке — "
            + (json.dumps(med["active"], ensure_ascii=False) if med["active"] else "ничего")
            + ". Курсы, начатые/завершённые в пределах 45 дн от сдачи — "
            + (json.dumps(med["changed_near_visit"], ensure_ascii=False) if med["changed_near_visit"] else "нет")
            + ". ОБЯЗАТЕЛЬНО проверь: не объясняется ли сдвиг маркера стартом или окончанием курса "
              "(влияние на печёночные пробы, липиды, глюкозу, электролиты, B12/гомоцистеин и т.п.)."
        )
        parts.append("Вне лабораторной нормы или заметный сдвиг: " + (json.dumps(d["abnormal"], ensure_ascii=False) if d["abnormal"] else "нет"))
        parts.append(
            "Задача: 1) назови 2-4 маркера, которые ЗАМЕТНО сдвинулись (не всё); 2) для каждого — вероятная связь "
            "с изменениями в питании/нагрузке/лекарствах за период; 3) что вне нормы и насколько ново; "
            "4) 2-3 гипотезы для обсуждения с врачом. Нет сдвигов — скажи одной фразой."
        )
    if d.get("new_abnormal"):
        parts.append("\n=== НОВЫЕ ОТКЛОНЕНИЯ (блок «🔬 Заметил:») ===")
        parts.append("; ".join(f"{a['name']} {a['value']} ({a['flag']}{', норма ' + a['ref'] if a.get('ref') else ''})" for a in d["new_abnormal"]))
    if d.get("new_overdue"):
        parts.append("\n=== ПРОСРОЧЕННЫЕ АНАЛИЗЫ (блок «📋 По плану анализов:») ===")
        parts.append("; ".join(f"{a['test']} — просрочен на {a['overdue_days']} дн (был на {a['due']})" for a in d["new_overdue"]))
    if d.get("new_sym_alerts"):
        parts.append("\n=== СИМПТОМ УСИЛИЛСЯ (блок «⚠️ По симптому:») ===")
        parts.append("; ".join(f"{a['symptom']} (тяжесть {a['severity']}, {a['change']})" for a in d["new_sym_alerts"]))
    if d.get("new_abnormal") or d.get("new_sym_alerts") or d.get("new_overdue"):
        parts.append('\nВ конце блоков про отклонения/симптомы/анализы добавь: «Если хочешь — напиши мне, разберём спокойно, нужно ли к врачу.»')
    if d.get("notify_read_failure") and d.get("read_failures"):
        parts.append("\n=== НЕ ПРОЧИТАНЫ ЛИСТЫ (блок «⚠️ Данные:») ===")
        parts.append("Сегодня не удалось прочитать: " + ", ".join(d["read_failures"]) + " (вероятно лимит Google Sheets). Проверка симптомов и анализов по этим листам НЕ проведена.")
        parts.append('Скажи пациенту коротко и без тревоги: «Сегодня часть данных не прочиталась — проверю завтра. Если у тебя недавно была сдача крови или появился/усилился симптом — напиши мне, разберём.»')
    if d.get("stale_thread"):
        st = d["stale_thread"]
        is_spine = bool(SPINE_RX.search(str(st.get("symptom") or "")))
        parts.append("\n=== ТЕМА ДАВНО НЕ ОБНОВЛЯЛАСЬ (блок «↩️ Раньше обсуждали:») ===")
        parts.append(f"Тема «{st['symptom']}» (статус {st['status']}) — последняя запись {st['age_days']} дн назад ({st['last_date']}).")
        parts.append(
            "Это тема про спину/грыжу — спроси КОНКРЕТНО два числа ОДНИМ коротким вопросом: сколько метров сегодня "
            "может пройти до онемения ноги, и сколько минут может просидеть до простреla. Без лишних слов."
            if is_spine else
            "Спроси ОДНИМ коротким человеческим вопросом, как сейчас с этим — стало лучше, так же или хуже. Без нагнетания."
        )
    if d.get("stale_investigations"):
        parts.append("\n(служебное, покажи мелким шрифтом в самом конце: расследования без обновления >30 дн — "
                      + ", ".join(d["stale_investigations"]) + " — считаю приостановленными.)")
    parts.append(
        "\nФормат: готовый текст для Telegram, ≤1400 знаков, теги <b> <i> допустимы, без Markdown. "
        "Начни с «🩺 <b>Разбор от системы</b>» если есть разбор анализов, иначе сразу с первого блока."
    )
    return "\n".join(parts)


def call_model(prompt: str, timeout: float = 30.0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return ""
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL, "temperature": 0.2, "max_tokens": 1400,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "reasoning": {"max_tokens": 600},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("health_watchdog: вызов модели упал")
        return ""


def run_once() -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        d = detect(cur)
    if not d["fire"]:
        with get_conn() as conn, conn.cursor() as cur:
            if d["_new_last_read_fail_notice"]:
                _store_state(cur, {}, None, d["_new_last_read_fail_notice"])
                conn.commit()
        return d

    llm_text = call_model(build_prompt(d))
    extra = f"\n\n#SYM:{d['stale_thread']['id']}" if d.get("stale_thread") else ""
    telegram.send_message(CHAT_ID, llm_text + extra, parse_mode="HTML")

    nudge_updates = {a["key"]: d["today"] for a in d["new_abnormal"]}
    nudge_updates.update({a["key"]: d["today"] for a in d["new_sym_alerts"]})
    nudge_updates.update({a["key"]: d["today"] for a in d["new_overdue"]})
    if d.get("stale_thread"):
        nudge_updates[d["stale_thread"]["key"]] = d["today"]
    new_last_reviewed_visit = d["newest_visit"]["id"] if (d["needs_lab_review"] and d["newest_visit"]) else None

    if d["needs_lab_review"]:
        note_text = (f"Автоматический разбор свежей сдачи ({d['newest_visit']['date']}). "
                     + re.sub(r"<[^>]+>", "", llm_text))[:1500]
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO health.doctor_notes (note_date, category, note, trigger, plan, doctor, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (d["today"], "Разбор анализов (система)", note_text, "новая сдача крови",
                 "обсудить с лечащим врачом", "AI-watchdog", "AI-watchdog"),
            )
            conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        _store_state(cur, nudge_updates, new_last_reviewed_visit, d["_new_last_read_fail_notice"])
        conn.commit()
    return d


def run_scheduler() -> None:
    logger.info("health_watchdog scheduler: старт")
    while True:
        try:
            now = datetime.now(VL)
            nxt = now.replace(hour=CHECK_HOUR_VL, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            run_once()
            run_log.mark_run("health_watchdog")
        except Exception as e:
            logger.exception("health_watchdog run_once упал — повтор завтра")
            alert_on_failure("health_watchdog", e)
            time.sleep(3600)
