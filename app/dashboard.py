"""Живые «сегодня»-метрики для дашборда — прямая замена n8n-кэша (2026-09-16).
См. также get_bioage_dashboard() ниже (2026-09-19, порт вкладки «Био-возраст»).

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
import json
import re
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
    for key, unit in (("steps_today_live", "шаг"), ("stress_today_live", "ед"), ("kcal_today_live", "ккал"), ("protein_today_live", "г")):
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
        "SELECT steps, stress, date, updated_at FROM health.live_steps_today "
        "WHERE date = (now() AT TIME ZONE 'Asia/Vladivostok')::date"
    )
    row = cur.fetchone()
    steps, stress, steps_date, steps_updated_at = (row if row else (None, None, None, None))

    return {
        "steps_today_live": int(steps) if steps is not None else None,
        "stress_today_live": int(stress) if stress is not None else None,
        "kcal_today_live": round(float(kcal_sum)) if kcal_sum is not None else None,
        "protein_today_live": round(float(protein_sum)) if protein_sum is not None else None,
        "meals_count_today": int(meal_count) if meal_count is not None else 0,
        "steps_source_date": steps_date.isoformat() if steps_date else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


# --- «Био-возраст»-экран: порт n8n Code-ноды "Build Bioage JSON" (2026-09-19) ---
#
# Выбран следующим для переноса не по алфавиту: единственный из активных
# дашборд-кэшей с НУЛЕВОЙ зависимостью от Google Sheets — все 5 источников
# (health.results/markers/visits/phenoage_log/lab_plan) уже в Postgres. Самый
# низкорисковый перенос из оставшихся, формула PhenoAge (Levine) НЕ
# дублируется — берётся готовой из phenoage_log, как и в оригинале (её
# считает отдельный воркфлоу PhenoAge Calc, ещё не портирован).

PHENO_MARKERS = {
    "alb": {"id": "M008", "rx": re.compile(r"альбумин", re.I), "label": "Альбумин"},
    "creat": {"id": "M004", "rx": re.compile(r"креатинин", re.I), "label": "Креатинин"},
    "gluc": {"id": "M003", "rx": re.compile(r"глюкоза", re.I), "label": "Глюкоза"},
    "crp": {"id": "M024", "rx": re.compile(r"с-реактивн|срб|crp", re.I), "label": "CRP (С-реактивный белок)"},
    "lymph": {"id": "M062", "rx": re.compile(r"лимфоциты\s*%", re.I), "label": "Лимфоциты %"},
    "mcv": {"id": "M043", "rx": re.compile(r"mcv|средний объ[её]м эритроцит", re.I), "label": "MCV"},
    "rdw": {"id": "M049", "rx": re.compile(r"rdw|ширина распред.*эритроцит", re.I), "label": "RDW"},
    "alp": {"id": "M017", "rx": re.compile(r"щелочн(ая)? фосфатаз|alp|щф", re.I), "label": "Щелочная фосфатаза"},
    "wbc": {"id": "M039", "rx": re.compile(r"лейкоциты|wbc", re.I), "label": "Лейкоциты (WBC)"},
}


def _d10_text(v) -> str:
    """Порт d10() из n8n: visits/phenoage_log — TEXT-колонки, дата может прийти
    и как DD.MM.YYYY, и как YYYY-MM-DD (наследие Sheets) — в отличие от
    daily_trends, тут не обычный ::date каст, нужен тот же regex, что в JS."""
    s = str(v or "").strip()
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return s[:10]


def _dec_year(iso: str):
    if not iso or len(iso) < 10:
        return None
    y, mo, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
    return round((y + ((mo - 1) * 30 + d) / 365) * 100) / 100


def _key_for_marker_id(mid, mark_by_id: dict):
    for key, definition in PHENO_MARKERS.items():
        if definition["id"] == mid:
            return key
        if definition["rx"].search((mark_by_id.get(mid) or {}).get("Name") or ""):
            return key
    return None


def _strip_pheno_prefix(s) -> str:
    return re.sub(r"^PhenoAge\s*/\s*", "", str(s or "")).strip()


def get_bioage_dashboard(cur) -> dict:
    cur.execute('SELECT "Visit_ID", "Marker_ID", "Value", "Lab_Min", "Lab_Max" FROM health.results')
    results = [{"Visit_ID": r[0], "Marker_ID": r[1], "Value": r[2], "Lab_Min": r[3], "Lab_Max": r[4]}
               for r in cur.fetchall()]

    cur.execute('SELECT "Marker_ID", "Name", "Category", "Standard_Unit", "Optimal_Min", "Optimal_Max" FROM health.markers')
    markers = [{"Marker_ID": r[0], "Name": r[1], "Category": r[2], "Standard_Unit": r[3],
                "Optimal_Min": r[4], "Optimal_Max": r[5]} for r in cur.fetchall()]
    mark_by_id = {m["Marker_ID"]: m for m in markers if m["Marker_ID"]}

    cur.execute('SELECT "Visit_ID", "Date", "Age_at_Visit" FROM health.visits')
    visits = [{"Visit_ID": r[0], "Date": r[1], "Age_at_Visit": r[2]} for r in cur.fetchall()]

    cur.execute(
        "SELECT date, chrono_age, phenoage, delta, markers_used, formula_version, "
        "contributions, marker_values, oldest_marker_date FROM health.phenoage_log"
    )
    pheno_log = [
        {"date": r[0], "chrono_age": r[1], "phenoage": r[2], "delta": r[3], "markers_used": r[4],
         "formula_version": r[5], "contributions": r[6], "marker_values": r[7], "oldest_marker_date": r[8]}
        for r in cur.fetchall()
    ]

    cur.execute(
        'SELECT "Plan_ID", "Test", "Category", "Interval_Months", "Last_Done", "Next_Due", '
        '"Reason", "Status", "Source", "Notes" FROM health.lab_plan'
    )
    lab_plan_rows = [
        {"Plan_ID": r[0], "Test": r[1], "Category": r[2], "Interval_Months": r[3], "Last_Done": r[4],
         "Next_Due": r[5], "Reason": r[6], "Status": r[7], "Source": r[8], "Notes": r[9]}
        for r in cur.fetchall()
    ]

    # visit -> {date, age, values:{key:val}}
    visit_map: dict = {}
    visit_date: dict = {}
    for v in visits:
        vid = v["Visit_ID"]
        visit_map[vid] = {"vid": vid, "date": _d10_text(v["Date"]), "age": _num(v["Age_at_Visit"]), "values": {}}
        visit_date[vid] = _d10_text(v["Date"])
    for r in results:
        key = _key_for_marker_id(r["Marker_ID"], mark_by_id)
        val = _num(r["Value"])
        if val is None:
            continue
        vid = r["Visit_ID"]
        if vid not in visit_map:
            visit_map[vid] = {"vid": vid, "date": visit_date.get(vid) or _d10_text(vid), "age": None, "values": {}}
        if key:
            visit_map[vid]["values"][key] = val

    age_anchor = None
    for v in sorted((v for v in visit_map.values() if v["date"] and v["age"] is not None), key=lambda v: v["date"]):
        age_anchor = {"date": v["date"], "age": v["age"]}

    def age_at_date(date_str):
        if age_anchor and date_str:
            dd = (datetime.fromisoformat(date_str) - datetime.fromisoformat(age_anchor["date"])).total_seconds() / (365.25 * 86400)
            return round((age_anchor["age"] + dd) * 10) / 10
        try:
            y = int((date_str or "")[:4])
        except ValueError:
            return None
        return y - 1982

    sorted_visits = sorted((v for v in visit_map.values() if v["date"]), key=lambda v: v["date"])

    today_vl = _dkey(datetime.now(timezone.utc) + timedelta(hours=10))
    cur_age = age_at_date(today_vl)
    key_label = {k: d["label"] for k, d in PHENO_MARKERS.items()}

    pl_valid = sorted(
        (r for r in pheno_log if _num(r["phenoage"]) is not None and r["formula_version"] and r["formula_version"] != "init"),
        key=lambda r: str(r["date"]), reverse=True,
    )
    pl_latest = pl_valid[0] if pl_valid else None

    drivers = []
    if pl_latest:
        pa = _num(pl_latest["phenoage"])
        chrono = _num(pl_latest["chrono_age"])
        if chrono is None:
            chrono = round(cur_age * 10) / 10 if cur_age is not None else None
        try:
            contrib = json.loads(pl_latest["contributions"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            contrib = {}
        try:
            mvals = json.loads(pl_latest["marker_values"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            mvals = {}
        delta = _num(pl_latest["delta"])
        if delta is None and pa is not None and chrono is not None:
            delta = round((pa - chrono) * 100) / 100
        phenoage = {
            "date": _d10_text(pl_latest["date"]), "value": pa, "chrono_age": chrono, "delta": delta,
            "formula_version": pl_latest["formula_version"], "missing": [],
            "oldest_marker_date": pl_latest["oldest_marker_date"] or None,
        }
        drivers.append({"label": "Хроно", "marker": None, "years": chrono, "type": "total"})
        for k in key_label:
            if contrib.get(k) is None:
                continue
            years = round(contrib[k] * 100) / 100
            mv = mvals.get(k) or {}
            drivers.append({
                "label": key_label[k], "marker": k, "years": years,
                "type": "pos" if years > 0 else ("neg" if years < 0 else "flat"),
                "value": mv.get("value"), "measured_date": mv.get("measured"),
            })
        drivers.append({"label": "PhenoAge", "marker": None, "years": pa, "type": "total"})
    else:
        phenoage = {
            "value": None, "chrono_age": round(cur_age * 10) / 10 if cur_age is not None else None,
            "delta": None, "missing": list(key_label.keys()),
            "note": "PhenoAge ещё не рассчитан — прогони PhenoAge Calc",
        }

    history = sorted(
        (
            {
                "date": _d10_text(r["date"]), "x": _dec_year(_d10_text(r["date"])),
                "chrono_age": _num(r["chrono_age"]), "phenoage": _num(r["phenoage"]), "delta": _num(r["delta"]),
                "kind": ("visit" if re.search("визит", r["markers_used"] or "")
                         else "estimated" if re.search("оценка", r["markers_used"] or "") else "current"),
            }
            for r in pheno_log if _num(r["phenoage"]) is not None and _d10_text(r["date"])
        ),
        key=lambda h: h["date"],
    )

    # таблица биомаркеров: PhenoAge-9 всегда + прочие с последним значением
    latest_any: dict = {}
    latest_any_date: dict = {}
    latest_any_range: dict = {}
    for v in sorted_visits:
        for r in (r for r in results if r["Visit_ID"] == v["vid"]):
            val = _num(r["Value"])
            if val is None:
                continue
            mid = r["Marker_ID"]
            latest_any[mid] = val
            latest_any_date[mid] = v["date"]
            latest_any_range[mid] = {"lab_min": _num(r["Lab_Min"]), "lab_max": _num(r["Lab_Max"])}

    pheno_ids = {d["id"] for d in PHENO_MARKERS.values()}
    biomarkers = []
    for m in markers:
        mid = m["Marker_ID"]
        if not mid or mid not in latest_any:
            continue
        val = latest_any[mid]
        opt_min, opt_max = _num(m["Optimal_Min"]), _num(m["Optimal_Max"])
        is_pheno = mid in pheno_ids
        if not is_pheno and opt_min is None and opt_max is None:
            continue
        rng = latest_any_range.get(mid) or {}
        biomarkers.append({
            "marker_id": mid, "label": m["Name"], "group": _strip_pheno_prefix(m["Category"]),
            "pheno": is_pheno, "value": val, "unit": m["Standard_Unit"] or None,
            "measured_date": latest_any_date.get(mid), "lab_min": rng.get("lab_min"), "lab_max": rng.get("lab_max"),
            "opt_min": opt_min, "opt_max": opt_max,
            "in_lab_range": (val >= rng["lab_min"] and val <= rng["lab_max"]) if (rng.get("lab_min") is not None and rng.get("lab_max") is not None) else None,
            "in_opt_range": (val >= opt_min and val <= opt_max) if (opt_min is not None and opt_max is not None) else None,
        })
    biomarkers.sort(key=lambda b: (not b["pheno"], b["group"] or "", b["label"] or ""))

    out_of_range = []
    for mid, val in latest_any.items():
        rng = latest_any_range.get(mid) or {}
        if rng.get("lab_min") is None and rng.get("lab_max") is None:
            continue
        low = rng.get("lab_min") is not None and val < rng["lab_min"]
        high = rng.get("lab_max") is not None and val > rng["lab_max"]
        if not low and not high:
            continue
        m = mark_by_id.get(mid) or {}
        out_of_range.append({
            "label": m.get("Name") or mid, "value": val, "unit": m.get("Standard_Unit"),
            "ref": f"{rng.get('lab_min', '')}–{rng.get('lab_max', '')}", "date": latest_any_date.get(mid),
            "flag": "ниже нормы" if low else "выше нормы",
        })
    out_of_range.sort(key=lambda o: o["date"] or "", reverse=True)

    def series(pattern):
        rx = re.compile(pattern, re.I)
        pts = sorted(
            (
                {"date": visit_date.get(r["Visit_ID"]) or _d10_text(r["Visit_ID"]), "value": _num(r["Value"])}
                for r in results if rx.search((mark_by_id.get(r["Marker_ID"]) or {}).get("Name") or "")
            ),
            key=lambda p: p["date"],
        )
        pts = [p for p in pts if p["date"] and p["value"] is not None]
        return pts[-12:]

    trends = {
        "wbc": series(r"лейкоциты|wbc"), "glucose": series(r"глюкоза"),
        "crp": series(r"с-реактивн|срб|crp"), "rdw": series(r"rdw|ширина распред.*эритроцит"),
    }

    rare_once = [
        PHENO_MARKERS[k]["label"] for k in PHENO_MARKERS
        if sum(1 for v in sorted_visits if v["values"].get(k) is not None) <= 1
    ]

    lab_plan = sorted(
        (
            {
                "plan_id": r["Plan_ID"], "test": r["Test"], "category": r["Category"] or "",
                "reason": r["Reason"] or "",
                "next_due": _d10_text(r["Next_Due"]) if len(_d10_text(r["Next_Due"])) == 10 else None,
                "days_left": (
                    round((datetime.fromisoformat(_d10_text(r["Next_Due"])) - datetime.fromisoformat(today_vl)).days)
                    if len(_d10_text(r["Next_Due"])) == 10 else None
                ),
                "interval_months": _num(r["Interval_Months"]),
                "last_done": _d10_text(r["Last_Done"]) if len(_d10_text(r["Last_Done"])) == 10 else None,
                "source": r["Source"] or "", "notes": r["Notes"] or "",
            }
            for r in lab_plan_rows
            if r["Test"] and str(r["Status"] or "active").lower() not in ("done", "paused", "archived")
        ),
        key=lambda p: p["next_due"] or "9999-99-99",
    )
    for p in lab_plan:
        p["overdue"] = p["days_left"] is not None and p["days_left"] < 0

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "phenoage": phenoage,
        "drivers": drivers,
        "history": history,
        "biomarkers": biomarkers,
        "out_of_range": out_of_range,
        "trends": trends,
        "lab_plan": lab_plan,
        "data_note": (
            "Маркеры с единственным измерением за всю историю (нет динамики): " + ", ".join(rare_once)
            + ". Для тренда нужна полная панель в одной сдаче."
        ) if rare_once else None,
    }


# --- «Сегодня»-экран: порт n8n Code-ноды "Build Today JSON" (2026-09-20) ---
#
# Последний из активных дашборд-кэшей ещё на Google Sheets. Doctor_Notes читается
# оригинальным Code-node, но нигде не используется в его выводе (проверено —
# ни одного обращения к переменной `notes` в 520 строках оригинала) — не
# переносим, переносить нечего. Остальные 3 листа, которых не было в Postgres
# (Patient_State — гейт нагрузки при грыже L5/S1, Action_Log — отметки "сделал"
# у плана, User_Profile — из него реально читается только поле
# "ОДА и неврология") перенесены тем же вечером (pg_schema_today_dashboard.sql +
# sheets_to_pg_mirror.js, ночной cron 40 4 * * * их подхватывает автоматически) —
# здесь читаем всё из Postgres, ни одного обращения к n8n/Sheets в рантайме.
#
# gate_transition_alert (алерт на снятие/возврат гейта нагрузки, A6, ревью Opus 5
# 2026-09-09) в JSON-ответе фронтенд нигде не рендерит (проверено grep'ом) — сам
# алерт как side-effect живёт отдельно, в app.gate_watch (эта функция здесь —
# чистая, без побочных эффектов, как get_bioage_dashboard/get_health_dashboard).

_HERNIA_RX = re.compile(r"грыж|радикулопат|протру[зи]|экструз|модик|modic|корешк", re.I)
_LOAD_RX = re.compile(
    r"интенсив|интервал|hiit|бег|пробеж|прыж|присед|становая|штанг|турник|подтяг|"
    r"отжим|планк|скручиван|макгил|ротац|наклон|подним|тяж(?:есть|ести|[её]л|\b)|"
    r"спринт|силов|кроссфит|бадминтон|берпи|выпад|растяж|мобилити|йог|лфк|упражнен|"
    r"качат|тренаж|скакалк|степ[- ]?аэроб|ударн|отягощ|гантел|гир(?:я|ю|ей)|планер",
    re.I,
)
_BUDGET_KEYS = ["Насыщенные жиры", "Добавленный сахар", "Натрий", "Кофеин", "Клетчатка", "Витамин D", "Кальций"]
_SLEEP_MIN_OK, _SLEEP_MAX_OK = 420, 540
_CRP_SENS, _MCV_SENS, _CRP_REF = 1.041, 0.292, 1.5
_STEPS_TARGET_DAILY = 10000  # общепринятая суточная норма (Han 2023), не персональная база — см. TODO в JS-оригинале


def _rows_as_dicts(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _parse_target_num(v):
    if v is None:
        return None
    s = str(v).strip()
    if re.match(r"^не\s", s, re.I):
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    return float(m.group(0).replace(",", ".")) if m else None


def _parse_actions(txt):
    m = re.search(r"<<<ACTIONS\s*([\s\S]*?)\s*ACTIONS>>>", str(txt or ""))
    if not m:
        return None
    try:
        o = json.loads(m.group(1).strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return o if isinstance(o, dict) and isinstance(o.get("actions"), list) else None


def _hhmm(minute) -> str:
    m = ((minute % 1440) + 1440) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _round4(x):
    return round(x * 10000) / 10000


def _action_id(issued, title) -> str:
    # порядок операций как в JS actionId(): slice(0,60) ДО схлопывания пробелов,
    # не после — иначе граница усечения может сместиться на длинных пробелах.
    t = re.sub(r"\s+", " ", str(title or "")[:60]).strip()
    return f"{issued or ''}|{t}"


def _load_gate(pstate: list[dict], profile: dict) -> dict:
    """Гейт безопасности (A1). Fail-safe: нет данных о снятии → ограничение
    действует. Уровни: 0 отдых · 1 ходьба · 2 +плавание · 3 умеренная аэробика ·
    4 интенсив. Порт 1:1 из loadGate() в оригинальном Code node."""
    active = [x for x in pstate
              if str(x.get("Status") or "").lower() == "active" and str(x.get("Contra_Load") or "").strip()]
    if active:
        x = max(active, key=lambda r: str(r.get("Confirmed_Date") or ""))
        swim = bool(re.search(r"плаван", str(x.get("Allowed") or ""), re.I))
        return {
            "cap": 2 if swim else 1, "blocked": True, "condition": x.get("Condition"),
            "contra": x.get("Contra_Load"), "allowed": x.get("Allowed") or "",
            "provokers": x.get("Provokers") or "", "review_due": x.get("Review_Due"),
            "source": (x.get("Source") or "карта пациента") + (f" от {x['Confirmed_Date']}" if x.get("Confirmed_Date") else ""),
        }

    oda = str(profile.get("ОДА и неврология") or "")
    prof_hernia = bool(_HERNIA_RX.search(oda)) and not re.search(
        r"ремисси|снят|полн(ое|ая) восстановлен|разрешена нагрузка", oda, re.I)
    prof_swim = prof_hernia and bool(re.search(r"плаван|бассейн", oda, re.I))

    # A6 fail-safe (ревью Opus 5, 2026-09-09): 0 строк в Patient_State = чтение
    # НЕ ПРОШЛО (синк не отработал), а НЕ «ограничений нет».
    if not pstate:
        return {
            "cap": 2 if prof_swim else 1, "blocked": True, "degraded": True,
            "condition": ("Грыжа/радикулопатия — Patient_State не прочитан, профиль подтверждает"
                          if prof_hernia else "Карта пациента не прочитана (Patient_State пуст)"),
            "contra": "осевая нагрузка, подъём тяжестей, скручивания, бег, прыжки, интервалы",
            "allowed": "ходьба" + (", плавание" if prof_swim else ""),
            "provokers": "", "review_due": None,
            "source": "⚠️ PATIENT_STATE НЕ ПРОЧИТАН" + (" (профиль подтверждает грыжу)" if prof_hernia else ""),
        }

    if prof_hernia:
        return {
            "cap": 2 if prof_swim else 1, "blocked": True,
            "condition": "Грыжа/радикулопатия (из профиля; в Patient_State активных ограничений нет)",
            "contra": "осевая нагрузка, подъём тяжестей, скручивания, бег/прыжки/интенсив",
            "allowed": "ходьба" + (", плавание" if prof_swim else ""),
            "provokers": "", "review_due": None,
            "source": "User_Profile (Patient_State без активных ограничений)",
        }

    return {"cap": 4, "blocked": False, "condition": None, "contra": None,
            "allowed": "", "provokers": "", "review_due": None, "source": None}


def _baseline(rows: list[dict], col: str, last_date: str, days: int):
    frm = (datetime.fromisoformat(last_date) - timedelta(days=days)).date().isoformat()
    vals = [v for v in (_num(r.get(col)) for r in rows if frm <= (r.get("Дата") or "") < last_date) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _load_mean(rows: list[dict], last_date: str, days: int):
    frm = (datetime.fromisoformat(last_date) - timedelta(days=days)).date().isoformat()
    vals = [(_num(r.get("Тренировка_Ккал")) or 0) for r in rows if (r.get("Дата") or "") > frm]
    return sum(vals) / len(vals) if vals else None


def get_today_dashboard(cur) -> dict:
    cur.execute('SELECT d.*, to_char(d."Дата", \'YYYY-MM-DD\') AS "Дата" FROM health.daily_trends d ORDER BY d."Дата"')
    daily = _rows_as_dicts(cur)

    cur.execute(
        "SELECT m.*, to_char(m.\"Date\" AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD\"T\"HH24:MI') AS \"Date\" "
        'FROM health.meals m '
        "WHERE (m.\"Date\" AT TIME ZONE 'Asia/Vladivostok')::date >= (now() AT TIME ZONE 'Asia/Vladivostok')::date - 3 "
        'ORDER BY m."Date"'
    )
    meals = _rows_as_dicts(cur)

    cur.execute('SELECT * FROM health.nutrient_targets')
    targets = _rows_as_dicts(cur)

    cur.execute('SELECT "Date", "Recommendation_Text" FROM health.recommendations_log')
    recs = _rows_as_dicts(cur)

    cur.execute('SELECT * FROM health.phenoage_log')
    pheno = _rows_as_dicts(cur)

    cur.execute('SELECT * FROM health.action_log')
    acts = _rows_as_dicts(cur)

    cur.execute('SELECT * FROM health.patient_state')
    pstate = _rows_as_dicts(cur)

    cur.execute('SELECT * FROM health.user_profile LIMIT 1')
    prof_rows = _rows_as_dicts(cur)
    profile = prof_rows[0] if prof_rows else {}

    # ---------- время ----------
    now_vl = datetime.now(timezone.utc) + timedelta(hours=10)
    today_iso = _dkey(now_vl)
    now_min = now_vl.hour * 60 + now_vl.minute

    rows = sorted((r for r in daily if r.get("Дата")), key=lambda r: r["Дата"])
    past_rows = [r for r in rows if r["Дата"] <= today_iso]
    last = (past_rows[-1] if past_rows else (rows[-1] if rows else {}))
    last_date = last.get("Дата") or today_iso

    # =====================================================================
    # 1. РЕШЕНИЕ ДНЯ — нагрузка или восстановление
    # =====================================================================
    bb = _num(last.get("Восстановление_BodyBattery"))
    hrv = _num(last.get("ВСР_ночная"))
    hrv_base = _baseline(rows, "ВСР_ночная", last_date, 30)
    hrv_delta = (hrv - hrv_base) if (hrv is not None and hrv_base is not None) else None

    acwr_garmin = _num(last.get("ACWR_Garmin"))
    acwr_status_g = (str(last.get("ACWR_Status") or "").strip().upper()) or None
    if acwr_garmin is not None:
        acwr, acwr_source = acwr_garmin, "garmin"
    else:
        acute, chronic = _load_mean(rows, last_date, 7), _load_mean(rows, last_date, 28)
        acwr = round((acute / chronic) * 100) / 100 if (acute is not None and chronic) else None
        acwr_source = "self" if acwr is not None else None
    load_high = (acwr_status_g == "HIGH") if acwr_status_g else (acwr is not None and acwr > 1.5)

    gate = _load_gate(pstate, profile)

    reasons = []
    if bb is not None:
        reasons.append({"label": "Body Battery", "value": bb, "judgment": "good" if bb >= 70 else "neutral" if bb >= 40 else "bad"})
    if hrv_delta is not None:
        reasons.append({
            "label": "ВСР к базе", "value": f"{'+' if hrv_delta > 0 else ''}{round(hrv_delta * 10) / 10} мс",
            "judgment": "neutral" if abs(hrv_delta) < 6 else ("good" if hrv_delta > 0 else "bad"),
        })
    if acwr is not None:
        reasons.append({
            "label": "ACWR (Garmin)" if acwr_source == "garmin" else "Нагрузка 7д/28д",
            "value": f"{acwr} · {acwr_status_g}" if acwr_status_g else acwr,
            "judgment": "bad" if load_high else ("neutral" if (acwr_status_g == "LOW" or acwr < 0.8) else "good"),
        })

    readiness = 0
    if bb is not None:
        hrv_bad = hrv_delta is not None and hrv_delta <= -6
        readiness = 4 if (bb >= 70 and not hrv_bad and not load_high) else (3 if (bb >= 40 and not load_high) else 1)
    no_garmin_today = bb is None and hrv is None and _num(last.get("Чистый_сон_мин")) is None

    if no_garmin_today:
        final_cap = min(gate["cap"] if gate["blocked"] else 2, 2)
    elif bb is None:
        final_cap = gate["cap"] if gate["blocked"] else 0
    else:
        final_cap = min(gate["cap"], readiness)
    cap_label = {0: "нет данных", 1: "только ходьба", 2: "ходьба и плавание", 3: "умеренная аэробика", 4: "можно интенсив"}
    verdict = cap_label[final_cap]

    if no_garmin_today:
        verdict_note = (
            f"Нет свежих данных Garmin + {gate['condition']}. Режим: {verdict}, ничего сверх, пока часы не синхронизируются."
            if gate["blocked"] else
            "Нет свежих данных Garmin (часы не синхронизировались?). Режим: ходьба и плавание, пока данные не появятся — вердикт по восстановлению не строим."
        )
    elif bb is None and not gate["blocked"]:
        verdict_note = "Не хватает данных Garmin за сегодня — держись лёгкой активности."
    elif gate["blocked"] and gate["cap"] <= readiness:
        reserve = "полный" if readiness >= 4 else "средний" if readiness >= 3 else "низкий"
        verdict_note = f"{gate['condition']}: {gate['contra']}. По восстановлению запас {reserve}, но это не отменяет ограничение по спине."
    elif final_cap >= 4:
        verdict_note = "Резерв восстановления есть, ВСР не просела, нагрузка не накоплена. Ограничений по здоровью нет."
    elif final_cap == 3:
        verdict_note = ("ВСР ниже твоей базы — аэробная работа без ударных интервалов." if (hrv_delta is not None and hrv_delta <= -6)
                         else "Резерв средний: аэробная работа без интенсива.")
    else:
        verdict_note = "Накоплена нагрузка — сегодня разгрузочный день." if load_high else "Низкий резерв восстановления — сегодня только лёгкая активность."

    decision = {
        "verdict": verdict, "note": verdict_note, "reasons": reasons,
        "gate": {
            "blocked": gate["blocked"], "condition": gate["condition"], "contra": gate["contra"],
            "allowed": gate["allowed"], "provokers": gate["provokers"],
            "review_due": gate["review_due"], "source": gate["source"], "degraded": bool(gate.get("degraded")),
        },
        "readiness_cap": readiness, "medical_cap": gate["cap"], "final_cap": final_cap,
        "no_garmin_today": no_garmin_today,
        "acwr": acwr, "acwr_status": acwr_status_g, "acwr_source": acwr_source, "load_high": load_high,
        "garmin_device": last.get("Garmin_устройство"), "training_status": last.get("Training_Status"),
        "caution": (f"{gate['condition']}. Разрешено: {gate['allowed'] or 'ходьба'}" if gate["blocked"] else None),
        "limit": gate["contra"] if gate["blocked"] else None,
    }

    # =====================================================================
    # 2. ОКНА ДНЯ
    # =====================================================================
    rec_sorted = sorted(
        (r for r in recs if r.get("Recommendation_Text") and (r.get("Date") or "")[:10] <= today_iso),
        key=lambda r: r.get("Date") or "", reverse=True,
    )
    latest_rec = rec_sorted[0] if rec_sorted else None
    latest_parsed = _parse_actions(latest_rec["Recommendation_Text"]) if latest_rec else None
    latest_actions = latest_parsed["actions"] if latest_parsed else []

    bed_min = 23 * 60 + 30
    for a in latest_actions:
        m = re.search(r"(\d{1,2})[:.](\d{2})", str(a.get("title") or ""))
        if m and re.search(r"отбо|спат|лож|сон", str(a.get("title") or ""), re.I):
            bed_min = int(m.group(1)) * 60 + int(m.group(2))
            break
    coffee_min = bed_min - 8 * 60
    meal_min = bed_min - 3 * 60

    def _window(key, label, target_min, hint):
        return {"key": key, "label": label, "until": _hhmm(target_min), "target_min": target_min, "hint": hint}

    windows = [
        _window("coffee", "Последний кофе", coffee_min, "за 8 ч до отбоя"),
        _window("meal", "Последняя еда", meal_min, "за 3 ч до отбоя"),
        _window("bed", "Отбой", bed_min,
                "из плана советника" if any(re.search(r"отбо|спат", str(a.get("title") or ""), re.I) for a in latest_actions)
                else "цель по умолчанию"),
    ]

    sleep_streak = 0
    for r in reversed(rows):
        v = _num(r.get("Чистый_сон_мин"))
        if v is not None and _SLEEP_MIN_OK <= v <= _SLEEP_MAX_OK:
            sleep_streak += 1
        else:
            break
    streaks = []
    if sleep_streak > 0:
        streaks.append({"label": "Сон в зоне 7–9 ч", "count": sleep_streak, "unit": "ночей"})

    def _daily_meal_sums(col):
        sums: dict = {}
        for m in meals:
            if not m.get("Date"):
                continue
            day = (m["Date"] or "")[:10]
            sums[day] = sums.get(day, 0) + (_num(m.get(col)) or 0)
        return sums

    def _limit_streak_days(col, cap):
        sums = _daily_meal_sums(col)
        streak = 0
        for r in reversed(rows[:-1]):
            day = r.get("Дата")
            if not day or day not in sums:
                break
            if sums[day] > cap:
                break
            streak += 1
        return streak

    for nutrient, streak_label in (("Натрий", "Соль в норме"), ("Добавленный сахар", "Сахар в норме"), ("Насыщенные жиры", "Жиры в норме")):
        t = next((x for x in targets if x.get("Нутриент") == nutrient), None)
        if not t or not t.get("Колонка_в_Meals"):
            continue
        cap = _parse_target_num(t.get("Верхний_предел_UL"))
        if not cap:
            continue
        s = _limit_streak_days(t["Колонка_в_Meals"], cap)
        if s > 0:
            streaks.append({"label": streak_label, "count": s, "unit": "дней"})

    alcohol_streak_days = 0
    for r in reversed(rows[:-1]):
        g = _num(r.get("Алкоголь_гр"))
        if g is None or g > 0:
            break
        alcohol_streak_days += 1
    if alcohol_streak_days > 0:
        streaks.append({"label": "Без алкоголя", "count": alcohol_streak_days, "unit": "дней"})

    # =====================================================================
    # 3. БЮДЖЕТ ДНЯ
    # =====================================================================
    today_meals = [m for m in meals if m.get("Date") and (m["Date"] or "")[:10] == today_iso]

    def _sum_col(col):
        return sum((_num(m.get(col)) or 0) for m in today_meals)

    budget = []
    for t in targets:
        name = t.get("Нутриент")
        if name not in _BUDGET_KEYS:
            continue
        col = t.get("Колонка_в_Meals")
        if not col:
            continue
        is_limit = "Риск избытка" in (t.get("Категория") or "")
        rda, ul = _parse_target_num(t.get("Норма_RDA_AI")), _parse_target_num(t.get("Верхний_предел_UL"))
        cap = ul if is_limit else rda
        if not cap:
            continue
        consumed = round(_sum_col(col) * 100) / 100
        pct = round((consumed / cap) * 100)
        budget.append({
            "label": name, "unit": t.get("Единица") or "", "kind": "limit" if is_limit else "goal",
            "consumed": consumed, "cap": cap, "pct": pct,
            "remaining": round((cap - consumed) * 100) / 100,
            "status": (("over" if pct > 100 else "close" if pct >= 80 else "ok") if is_limit
                       else ("done" if pct >= 100 else "partial" if pct >= 60 else "low")),
        })
    budget.sort(key=lambda b: (0 if b["kind"] == "limit" else 1, -b["pct"]))

    kcal_today = round(_sum_col("Calories"))
    protein_today = round(_sum_col("Proteins"))

    # =====================================================================
    # 4. ПЛАН НЕДЕЛИ
    # =====================================================================
    done_map = {}
    for a in acts:
        if a.get("Action_ID"):
            done_val = a.get("Done")
            done_map[str(a["Action_ID"])] = str(done_val or "").lower() in ("да", "true") or done_val is True
    issued = (latest_rec.get("Date") or "")[:10] if latest_rec else None

    allowed_lc = str(gate["allowed"] or "").lower()
    g_allow_walk = bool(re.search(r"ходьб|walk|прогул", allowed_lc))
    g_allow_swim = bool(re.search(r"плаван|бассейн|swim", allowed_lc))

    def _action_conflicts(a):
        if not gate["blocked"]:
            return False
        t = str(a.get("type") or "").lower()
        if t in ("load_high", "load_low"):
            return True
        if t == "walk":
            return not g_allow_walk
        if t == "swim":
            return not g_allow_swim
        if _LOAD_RX.search(f"{a.get('title')} {a.get('why')}"):
            return True
        if gate.get("degraded") and t in ("", "unknown"):
            return True
        return False

    plan = {
        "date": issued,
        "actions": [
            {
                "id": _action_id(issued, a.get("title")),
                "title": a.get("title"), "why": a.get("why") or "", "expect": a.get("expect") or "",
                "priority": a.get("priority") or "средний", "metric": a.get("metric"), "type": a.get("type"),
                "done": bool(done_map.get(_action_id(issued, a.get("title")))),
                "blocked_by_gate": _action_conflicts(a),
                "gate_note": (f"Противоречит ограничению: {gate['condition']}. Разрешено только: {gate['allowed'] or 'ходьба'}"
                              if _action_conflicts(a) else None),
            }
            for a in latest_actions
        ],
    }

    # =====================================================================
    # 5. ДЛЯ ДОЛГОЛЕТИЯ
    # =====================================================================
    pheno_valid = sorted((p for p in pheno if _num(p.get("phenoage")) is not None and p.get("date")),
                         key=lambda p: p["date"], reverse=True)
    ph = pheno_valid[0] if pheno_valid else None

    sleep_min_today = _num(last.get("Чистый_сон_мин"))
    steps_today = _num(last.get("Шаги_за_вчера"))
    alcohol_g_today = _num(last.get("Алкоголь_гр")) or 0
    fiber_b = next((b for b in budget if b["label"] == "Клетчатка"), None)
    sat_fat_b = next((b for b in budget if b["label"] == "Насыщенные жиры"), None)
    sugar_b = next((b for b in budget if b["label"] == "Добавленный сахар"), None)
    affects = []

    if sleep_min_today is not None:
        lo, hi = 420, 540
        years, what = 0, None
        if sleep_min_today < lo:
            years = 0.582 * min(1, (lo - sleep_min_today) / 180) / 365
            what = f"сон {sleep_min_today / 60:.1f} ч — короче нормы"
        elif sleep_min_today > hi:
            years = 0.694 * min(1, (sleep_min_today - hi) / 120) / 365
            what = f"сон {sleep_min_today / 60:.1f} ч — длиннее нормы"
        else:
            what = "сон в зоне 7–9 ч"
        affects.append({
            "what": what,
            "how": "Короткий/длинный сон системно повышает СРБ (воспалительный маркер формулы) — но нужно ≥3 ночи подряд, разовая ночь почти не в счёт. Источник: Ballesio 2025, You 2024 (NHANES).",
            "markers": ["crp"], "direction": "up" if years > 0.00005 else "down" if years < -0.00005 else "neutral",
            "weight": "unknown" if years == 0 else "moderate", "est_years": None if years == 0 else _round4(years),
        })

    if steps_today is not None:
        delta_steps = steps_today - _STEPS_TARGET_DAILY
        years = -3.98 * ((delta_steps / 100) / 30) / 365
        affects.append({
            "what": f"{'+' if delta_steps >= 0 else ''}{round(delta_steps)} шагов к норме {_STEPS_TARGET_DAILY}",
            "how": "Замена сидения на движение снижает СРБ/лейкоциты/RDW — самая воспроизводимая связь в базе (2 независимых NHANES-анализа). Источник: Han 2023.",
            "markers": ["crp", "wbc", "rdw"], "direction": "up" if years > 0.00005 else "down" if years < -0.00005 else "neutral",
            "weight": "strong", "est_years": _round4(years),
        })

    mcv_shift_fl = (0.30 * (alcohol_g_today / 40) / 100) * 88
    years = (mcv_shift_fl * _MCV_SENS) / (90 / 7)
    affects.append({
        "what": f"{alcohol_g_today} г алкоголя сегодня" if alcohol_g_today > 0 else "без алкоголя сегодня",
        "how": "Алкоголь линейно повышает MCV — причинная связь (менделевская рандомизация, UK Biobank). Эффект накапливается за ~90 дней оборота эритроцитов. Источник: Thompson 2021.",
        "markers": ["mcv"], "direction": "up" if years > 0.00002 else "neutral",
        "weight": "moderate" if alcohol_g_today > 0 else "unknown", "est_years": _round4(years),
    })

    if fiber_b:
        gap_g = fiber_b["cap"] - fiber_b["consumed"]
        years = (gap_g / 8) * (0.37 * _CRP_SENS / _CRP_REF) / 42
        affects.append({
            "what": f"клетчатка {fiber_b['consumed']}/{fiber_b['cap']} г",
            "how": "Клетчатка снижает СРБ — подтверждено в 7+ независимых RCT/метаанализах. Источник: Jiao 2015, Jain 2025.",
            "markers": ["crp"], "direction": "up" if years > 0.00002 else "down" if years < -0.00002 else "neutral",
            "weight": "moderate", "est_years": _round4(years),
        })

    if sat_fat_b and sugar_b:
        proxy_pct = (sat_fat_b["pct"] - 100) + (sugar_b["pct"] - 100)
        years = (0.21 * proxy_pct / 10) / 365
        affects.append({
            "what": f"насыщ. жиры {sat_fat_b['pct']}%, сахар {sugar_b['pct']}% от лимита",
            "how": "Хронический избыток насыщенных жиров/сахара связан с ростом PhenoAge через глюкозу и слабее СРБ, но за один день эффект почти не заметен (нужны недели) — самый слабый по доказательности пункт формулы. Источник: Cardoso 2024.",
            "markers": ["gluc", "crp"], "direction": "up" if years > 0.00002 else "down" if years < -0.00002 else "neutral",
            "weight": "weak", "est_years": _round4(years),
        })

    affects_total = _round4(sum(a["est_years"] or 0 for a in affects))

    if ph:
        longevity = {
            "phenoage": _num(ph.get("phenoage")), "chrono_age": _num(ph.get("chrono_age")), "delta": _num(ph.get("delta")),
            "computed": (ph.get("date") or "")[:10],
            "affects_today": affects, "affects_today_total": affects_total,
            "affects_today_disclaimer": "Оценка направления и порядка величины по поведенческой литературе, не пересчёт настоящего PhenoAge (тот считается только по анализам крови).",
        }
    else:
        longevity = None

    # =====================================================================
    # 6. СПОКОЙНО
    # =====================================================================
    quiet = []
    for col, (lo, hi) in {"SpO2_ночь_среднее": (95, 100), "Дыхание_ночь_среднее": (12, 20)}.items():
        v = _num(last.get(col))
        if v is not None and lo <= v <= hi:
            quiet.append("SpO2" if col == "SpO2_ночь_среднее" else "дыхание")
    if _num(last.get("Пульс_ночной_средний")) is not None:
        rb = _baseline(rows, "Пульс_ночной_средний", last_date, 30)
        rv = _num(last.get("Пульс_ночной_средний"))
        if rb is not None and abs(rv - rb) < 3:
            quiet.append("пульс покоя")
    if _num(last.get("Стресс_дневной_средний")) is not None:
        sb = _baseline(rows, "Стресс_дневной_средний", last_date, 30)
        sv = _num(last.get("Стресс_дневной_средний"))
        if sb is not None and sv - sb < 6:
            quiet.append("стресс")

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "date": today_iso, "now_local": _hhmm(now_min), "data_date": last_date,
        "decision": decision, "windows": windows, "streaks": streaks,
        "budget": budget, "kcal_today": kcal_today, "protein_today": protein_today, "meals_today": len(today_meals),
        "plan": plan, "longevity": longevity, "quiet": quiet,
    }
