"""Живые «сегодня»-метрики для дашборда — прямая замена n8n-кэша (2026-09-16).

Контекст: `health-dashboard (cache)` в n8n считал steps_today_live/kcal_today_live/
protein_today_live через Schedule Trigger раз в 15/30 минут, писал в
$getWorkflowStaticData. Обнаружили две независимые причины «сегодня» зависало на
2 дня: (1) сам Schedule Trigger не срабатывал на интервале 30 мин (сработал на 1 и
15 — конкретная причина не разобрана до конца, эмпирически подтверждено на живом
n8n через тестовый воркфлоу), и (2) даже когда триггер работал, kcal/protein
считались из `health.day_sum` — таблицы, которая оказалась заброшенным снапшотом
миграции с апреля (`_synced_at: 2026-09-10`, ни разу не обновлялась после).

По прямому решению Влада («получение данных на питон, забудь про n8n») — эти три
метрики больше не проходят ни через какое расписание вообще: считаются заново на
каждый запрос, прямо из живых таблиц. Обновляться раз в сутки/навсегда зависать
такой код структурно не может — нет ни кэша, ни промежуточного состояния.

kcal/protein — сумма health.meals за сегодняшний календарный день по Владивостоку
(тот же фильтр, что уже в app.doctor.tools.get_meals_today — специально НЕ
2-часовой сдвиг из analyze_nutrition_stability, тот сдвиг для недельных средних,
здесь нужен обычный календарный день). steps — последняя строка
health.live_steps_today (её пишет напрямую push_live_steps.py с гарминбота,
см. STATE.md 2026-09-16 — тоже больше не через n8n).
"""
from datetime import date, datetime, timedelta, timezone

# --- «Здоровье»-экран: порт n8n Code-ноды "Build Health JSON" (2026-09-16) ---
#
# Причина порта та же, что у get_today_live_metrics выше: health-dashboard (cache)
# в n8n держал ВЕСЬ этот расчёт (не только три live-метрики) в staticData за
# Schedule Trigger, который оказался ненадёжен на 30-минутном интервале — экран
# «Здоровье» показывал данные на 2 дня младше, чем есть на самом деле.
#
# Metric_Config — лист Google Sheets, из которого n8n брал конфиг метрик
# (направление/личный-базовый-порог/целевая зона). Меняется он крайне редко
# (последний раз — задолго до этой сессии) и прямого доступа к Google Sheets из
# card-service нет (см. STATE.md 2026-09-16 — блокер именно в credentials, не в
# лени) — поэтому здесь одноразовый снапшот, полученный экспортом через
# временный n8n-воркфлоу (не runtime-зависимость, чистый bootstrap данных).
# Если реально поменяется лист — обновить этот список руками, он не читается
# ниоткуда автоматически.
METRIC_CONFIG = [
    {"key": "hrv", "col": "ВСР_ночная", "label": "ВСР ночью", "unit": "мс",
     "direction": "higher_better", "min_abs_delta": 6, "kind": "baseline"},
    {"key": "rhr", "col": "Пульс_ночной_средний", "label": "Пульс покоя", "unit": "уд/мин",
     "direction": "lower_better", "min_abs_delta": 3, "kind": "baseline"},
    {"key": "body_battery", "col": "Восстановление_BodyBattery", "label": "Body Battery", "unit": "",
     "direction": "higher_better", "min_abs_delta": 12, "kind": "baseline"},
    {"key": "sleep_min", "col": "Чистый_сон_мин", "label": "Чистый сон", "unit": "мин",
     "direction": "higher_better", "min_abs_delta": 30, "kind": "baseline",
     "target_min": 420, "target_max": 540, "target_label": "7–9 ч"},
    {"key": "sleep_score", "col": "Оценка_сна_балл", "label": "Оценка сна", "unit": "балл",
     "direction": "higher_better", "min_abs_delta": 7, "kind": "baseline"},
    {"key": "sleep_eff", "col": "Эффективность_сна_", "label": "Эффективность сна", "unit": "%",
     "direction": "higher_better", "min_abs_delta": 4, "kind": "baseline",
     "target_min": 85, "target_max": 100, "target_label": "85–100%"},
    {"key": "stress", "col": "Стресс_дневной_средний", "label": "Стресс дневной", "unit": "",
     "direction": "lower_better", "min_abs_delta": 6, "kind": "baseline"},
    {"key": "steps", "col": "Шаги_за_вчера", "label": "Шаги", "unit": "",
     "direction": "neutral", "min_abs_delta": 3500, "kind": "baseline"},
    {"key": "vo2max", "col": "VO2_Max", "label": "VO2 Max", "unit": "",
     "direction": "higher_better", "min_abs_delta": 2, "kind": "baseline"},
    {"key": "kcal", "col": "Питание_Всего_Ккал", "label": "Калории", "unit": "ккал",
     "direction": "neutral", "min_abs_delta": 350, "kind": "baseline"},
    {"key": "protein", "col": "Питание_Всего_Белки_г", "label": "Белок", "unit": "г",
     "direction": "higher_better", "min_abs_delta": 25, "kind": "baseline"},
    {"key": "respiration", "col": "Дыхание_ночь_среднее", "label": "Дыхание ночью", "unit": "/мин",
     "direction": "reference", "kind": "reference", "target_min": 12, "target_max": 20},
    {"key": "spo2", "col": "SpO2_ночь_среднее", "label": "SpO2 ночью", "unit": "%",
     "direction": "reference", "kind": "reference", "target_min": 95, "target_max": 100},
]
COL = {m["key"]: m["col"] for m in METRIC_CONFIG}
METRICS = [m for m in METRIC_CONFIG if m["kind"] == "baseline"]
REFERENCE_METRICS = [m for m in METRIC_CONFIG if m["kind"] == "reference"]
TARGET_ZONES = {
    m["key"]: {"min": m["target_min"], "max": m["target_max"], "label": m.get("target_label", "")}
    for m in METRIC_CONFIG if m.get("target_min") is not None and m.get("target_max") is not None
}

# Обновление 2026-09-16: временный мост на n8n убран. Anomaly_Detector/
# Correlations (n8n, 4DE8Hg832E2nn8MM) теперь дополнительно к Google Sheets
# пишет health.anomaly_log (backups/infra/migrate_anomaly_log.sql) — сама
# детекция осталась в n8n (не рерайт математики, только новый сток данных),
# но ЧТЕНИЕ для дашборда — целиком отсюда, без единого обращения к n8n.
#
# correlations/experiments — не забытые поля: в исходном n8n-коде они были
# буквально захардкожены отключёнными («движок корреляций отключён — слепой
# перебор пар на малых данных = шум», коллективное ревью, решение уже принято
# раньше), реального источника данных для них никогда не было. `recommendation`
# (совет от Weekly AI Advisor, Google Sheets) в JSON был, но фронтенд его нигде
# не рендерит — проверено (`grep` по v4.html/index.html), поэтому не переносил.
_DISABLED_CORRELATIONS = {"computed": None, "disabled": True, "priority": [], "discovery": []}
_EXPERIMENTS_NOTE = (
    "Движок корреляций отключён (слепой перебор пар на малых данных = шум). "
    "Гипотезы о причинах теперь ведёт доктор-бот: связь симптомов с едой + "
    "элиминационные тесты."
)


def _r1(x):
    return None if x is None else round(x * 10) / 10


def _r_smart(x):
    if x is None:
        return None
    return round(x) if abs(x) >= 100 else round(x * 10) / 10


def _dkey(d) -> str:
    if isinstance(d, (date, datetime)):
        return d.isoformat()[:10]
    return str(d or "")[:10]


def _judge(direction, delta_abs, min_abs_delta):
    if direction == "neutral":
        return "neutral"
    if delta_abs is None or min_abs_delta is None or abs(delta_abs) < min_abs_delta:
        return "neutral"
    better = delta_abs > 0 if direction == "higher_better" else delta_abs < 0
    return "good" if better else "bad"


def _baseline_for(rows, col_idx, upto_idx, window_days, min_points):
    cur_date = rows[upto_idx][0]  # rows[i] = (Дата, {col: val, ...})
    frm = cur_date - timedelta(days=window_days)
    vals = [rows[i][1].get(col_idx) for i in range(upto_idx) if frm <= rows[i][0] < cur_date]
    vals = [v for v in vals if v is not None]
    if len(vals) < min_points:
        return None
    mean = sum(vals) / len(vals)
    std = None
    if len(vals) > 1:
        std = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    return {"mean": mean, "std": std, "n": len(vals), "days": window_days}


BASE_WINDOWS = [(30, 10), (7, 4)]


def get_health_dashboard(cur) -> dict:
    cols_needed = sorted(set(COL.values()))
    col_select = ", ".join(f'"{c}"' for c in cols_needed)
    cur.execute(f'SELECT "Дата", {col_select} FROM health.daily_trends ORDER BY "Дата" ASC')
    raw_rows = cur.fetchall()
    if not raw_rows:
        return {"error": "no_data"}

    rows = []
    for r in raw_rows:
        d = r[0]
        parsed = {col: _num(r[i + 1]) for i, col in enumerate(cols_needed)}
        rows.append((d, parsed))

    last_date, last_row = rows[-1]
    today_parsed = {k: last_row.get(col) for k, col in COL.items()}

    metrics = []
    metric_by_key = {}
    for m in METRICS:
        value = last_row.get(m["col"])
        base = None
        for window_days, min_points in BASE_WINDOWS:
            base = _baseline_for(rows, m["col"], len(rows) - 1, window_days, min_points)
            if base:
                break
        delta_abs = (value - base["mean"]) if (value is not None and base) else None
        delta_pct = (delta_abs / base["mean"] * 100) if (delta_abs is not None and base["mean"]) else None
        entry = {
            "key": m["key"], "label": m["label"], "unit": m["unit"],
            "value": _r_smart(value), "baseline": _r_smart(base["mean"]) if base else None,
            "baseline_days": base["days"] if base else None, "baseline_n": base["n"] if base else None,
            "delta_abs": _r_smart(delta_abs), "delta_pct": round(delta_pct) if delta_pct is not None else None,
            "z": round((value - base["mean"]) / base["std"], 2) if (value is not None and base and base["std"]) else None,
            "direction": m["direction"], "judgment": _judge(m["direction"], delta_abs, m["min_abs_delta"]),
            "kind": "baseline",
        }
        metrics.append(entry)
        metric_by_key[m["key"]] = entry

    for m in REFERENCE_METRICS:
        value = last_row.get(m["col"])
        judgment = "neutral"
        if value is not None:
            judgment = "good" if (m["target_min"] <= value <= m["target_max"]) else "bad"
        entry = {
            "key": m["key"], "label": m["label"], "unit": m["unit"],
            "value": _r1(value), "ref_min": m["target_min"], "ref_max": m["target_max"],
            "baseline": None, "delta_abs": None, "delta_pct": None,
            "direction": "reference", "judgment": judgment, "kind": "reference",
        }
        metrics.append(entry)
        metric_by_key[m["key"]] = entry

    live = get_today_live_metrics(cur)
    for key, unit in (("steps_today_live", "шаг"), ("kcal_today_live", "ккал"), ("protein_today_live", "г")):
        value = live.get(key)
        metrics.append({
            "key": key, "label": key, "unit": unit, "value": value,
            "baseline": None, "baseline_days": None, "baseline_n": None,
            "delta_abs": None, "delta_pct": None, "z": None,
            "direction": "reference", "judgment": "neutral", "kind": "live",
        })

    cfg_by_key = {m["key"]: m for m in METRIC_CONFIG}
    days_14 = []
    for d, parsed in rows[-14:]:
        o = {"date": _dkey(d)}
        for key, col in COL.items():
            v = parsed.get(col)
            o[key] = v
            met = metric_by_key.get(key)
            cfg = cfg_by_key.get(key)
            if met and met.get("baseline") is not None and v is not None:
                o[f"{key}_j"] = _judge(cfg["direction"], v - met["baseline"], cfg.get("min_abs_delta"))
            elif key in TARGET_ZONES and v is not None:
                z = TARGET_ZONES[key]
                o[f"{key}_j"] = "good" if (z["min"] <= v <= z["max"]) else "bad"
            else:
                o[f"{key}_j"] = "neutral"
        days_14.append(o)

    week, prev_week = rows[-7:], rows[-14:-7]

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    # как в оригинале: cfg ищется только среди METRICS (baseline), не всего
    # METRIC_CONFIG — для reference-ключей (respiration/spo2) direction честно
    # 'neutral', вторую логику направления для них никто не считал и в n8n.
    metrics_by_key = {m["key"]: m for m in METRICS}
    trends = {}
    for key, col in COL.items():
        a = _avg(p.get(col) for _, p in week)
        b = _avg(p.get(col) for _, p in prev_week)
        delta_abs = (a - b) if (a is not None and b is not None) else None
        delta_pct = round((a - b) / b * 100) if (delta_abs is not None and b) else None
        cfg = metrics_by_key.get(key)
        direction = cfg["direction"] if cfg else "neutral"
        judgment = _judge(direction, delta_abs, cfg.get("min_abs_delta")) if cfg else "neutral"
        trends[key] = {"this_week": _r_smart(a), "prev_week": _r_smart(b),
                        "delta_abs": _r_smart(delta_abs), "delta_pct": delta_pct,
                        "direction": direction, "judgment": judgment}

    inv_since = last_date - timedelta(days=60)
    cur.execute(
        "SELECT inv_id, opened, updated, closed, status, trigger, hypothesis, findings, "
        "doctor_brief, referral FROM health.investigations "
        "WHERE status = 'open' OR (updated IS NOT NULL AND updated >= %s) "
        "ORDER BY COALESCE(updated, opened) DESC LIMIT 10",
        (inv_since,),
    )
    investigations = [
        {"id": r[0], "opened": _dkey(r[1]) or None, "updated": _dkey(r[2]) or None,
         "closed": _dkey(r[3]) if r[3] else None, "status": r[4], "trigger": r[5] or "",
         "hypothesis": r[6] or "", "findings": r[7] or "", "doctor_brief": r[8] or "", "referral": r[9] or ""}
        for r in cur.fetchall()
    ]

    notes_since = last_date - timedelta(days=120)
    cur.execute(
        "SELECT note_date, category, note FROM health.doctor_notes "
        "WHERE note_date >= %s AND note_date <= %s AND note IS NOT NULL "
        "ORDER BY note_date DESC LIMIT 15",
        (notes_since, last_date),
    )
    medical_notes_recent = [{"date": _dkey(r[0]), "category": r[1], "note": r[2]} for r in cur.fetchall()]

    # --- аномалии: последняя запись health.anomaly_log + история метрики за 14 дней ---
    cur.execute(
        "SELECT date, anomaly_count, strong_count, raw_anomalies FROM health.anomaly_log "
        "ORDER BY date DESC LIMIT 1"
    )
    latest_anom = cur.fetchone()
    anomalies = {"report_date": None, "count": 0, "strong_count": 0, "items": []}
    if latest_anom:
        anom_date, anomaly_count, strong_count, raw = latest_anom
        raw = raw or []  # jsonb — уже питоновский list/dict, ручной json.loads не нужен
        hist14 = rows[-14:]
        items = []
        for a in raw:
            metric = a.get("metric")
            history = []
            for _, parsed in hist14:
                v = parsed.get(metric)
                # «Эффективность_сна_»: 100% иногда приходит как доля 1, не «100»
                # (тот же нюанс, что был в n8n-версии — не потерять при переносе).
                if v is not None and metric == "Эффективность_сна_" and v <= 1.5:
                    v = v * 100
                if v is not None:
                    history.append(v)
            items.append({
                "metric": metric, "label": a.get("label") or metric,
                "direction": a.get("direction"), "severity": a.get("severity"),
                "interpretation": a.get("interpretation"), "baseline_mean": _num(a.get("baseline_mean")),
                "history": history,
            })
        anomalies = {
            "report_date": _dkey(anom_date), "count": anomaly_count or len(raw),
            "strong_count": strong_count or 0, "items": items,
        }

    # Три явных состояния (запрос Влада, было и в n8n-версии): flagged —
    # сегодняшняя проверка что-то нашла; clean — прошла и чисто; not_run —
    # сегодняшних данных ещё нет, проверке не из чего было считать.
    today_vl = _dkey((datetime.now(timezone.utc) + timedelta(hours=10)))
    if anomalies["report_date"] == today_vl:
        anomalies["status"] = "flagged"
    elif _dkey(last_date) == today_vl:
        anomalies = {"report_date": today_vl, "count": 0, "strong_count": 0, "items": [], "status": "clean"}
    else:
        anomalies = {"report_date": None, "count": 0, "strong_count": 0, "items": [], "status": "not_run"}

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"from": days_14[0]["date"] if days_14 else None, "to": _dkey(last_date)},
        "today": {"date": _dkey(last_date), "parsed": today_parsed},
        "metrics": metrics,
        "days_14": days_14,
        "trends": trends,
        "investigations": investigations,
        "medical_notes_recent": medical_notes_recent,
        "anomalies": anomalies,
        "correlations": _DISABLED_CORRELATIONS,
        "experiments": [],
        "experiments_note": _EXPERIMENTS_NOTE,
    }
    return result


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def get_today_live_metrics(cur) -> dict:
    # health.meals."Calories"/"Proteins" — TEXT, не numeric (то же наступление,
    # что и totalFats.toFixed в n8n Dashboard Cached, см. STATE.md 2026-09-16) —
    # SQL SUM() падает с UndefinedFunction; складываем в Python через _num(),
    # как уже сделано в get_meals_today (app/doctor/tools.py).
    cur.execute(
        "SELECT \"Calories\", \"Proteins\" FROM health.meals "
        "WHERE (\"Date\" AT TIME ZONE 'Asia/Vladivostok')::date = (now() AT TIME ZONE 'Asia/Vladivostok')::date"
    )
    meal_rows = cur.fetchall()
    kcal_sum = sum(v for v in (_num(r[0]) for r in meal_rows) if v is not None)
    protein_sum = sum(v for v in (_num(r[1]) for r in meal_rows) if v is not None)
    meal_count = len(meal_rows)

    cur.execute(
        "SELECT steps, date, updated_at FROM health.live_steps_today "
        "WHERE date = (now() AT TIME ZONE 'Asia/Vladivostok')::date"
    )
    row = cur.fetchone()
    steps, steps_date, steps_updated_at = (row if row else (None, None, None))

    return {
        "steps_today_live": int(steps) if steps is not None else None,
        "kcal_today_live": round(float(kcal_sum)) if kcal_sum is not None else None,
        "protein_today_live": round(float(protein_sum)) if protein_sum is not None else None,
        "meals_count_today": int(meal_count) if meal_count is not None else 0,
        "steps_source_date": steps_date.isoformat() if steps_date else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
