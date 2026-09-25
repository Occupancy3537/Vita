"""Порт n8n `Anomaly_Detector/Correlations` (2026-09-20, группа 3, первая
половина — переносится вместе с `Collect_Biohacking_Data`, см.
app/biohacking_ingest.py, потому что они реально связаны: коллектор зовёт
детектор через executeWorkflow после каждой записи в Daily_Trends — #30
уже нашёл эту связку и намеренно отложил обе части до одного захода).

Три источника запуска в оригинале, все три сохранены:
1. Сразу после ингеста Гармина (было executeWorkflowTrigger "Триггер: пришли
   данные" из Collect_Biohacking_Data) → здесь прямой вызов
   `run_daily_check()` из biohacking_ingest.py, тот же процесс.
2. Ежедневно 09:15 ВЛ (было Daily Schedule Trigger) → `run_scheduler()`.
3. Еженедельно по воскресеньям 11:00 ВЛ (было Weekly Schedule Trigger,
   подтверждено по execution_entity: оба недавних прогона — 13 и 20
   сентября — воскресенье) → `run_weekly_scheduler()`.

Z-score движок (детект отклонений по 7/30/90-дневным окнам с гейтом
минимальной абсолютной дельты) перенесён 1:1, включая гейт на шум
измерения у почти-константных метрик (VO2 Max и т.п.) — z может быть
большим при узком baseline, но если абсолютное отклонение меньше
minAbsDelta, это не аномалия.

НАЙДЕНО при переносе, унифицировано (не поведенческий баг, но дублирование
с риском будущего дрейфа): в оригинале ДВА независимых Code-узла с ОДНИМ
и тем же алгоритмом — "Anomaly Detection" (дневной+event путь) читает
конфиг метрик из листа Metric_Config с фолбэком на хардкод, а "Anomaly
Detection1" (недельный путь) ВСЕГДА использовал только хардкод, никогда не
читал Metric_Config. Сверил содержимое листа с хардкодом (2026-09-20) —
сейчас они идентичны, поведенческой разницы сегодня нет. Порт использует
ОДНУ функцию `_load_metrics()` для обоих путей (дневного и недельного) —
устраняет риск будущего тихого расхождения, если Metric_Config когда-нибудь
поправят, а недельный путь не заметит.

Дедуп Telegram-алертов был в $getWorkflowStaticData('global').alerted{}
(ключ дата+метка метрики, чистка когда дат становится >2) → теперь в
health.anomaly_alerted (key = "<date>|<label>"), чистка по возрасту
(> ANOMALY_ALERT_KEEP_DAYS дней), тот же эффект, переживает рестарт
контейнера, чего $getWorkflowStaticData и так не переживал между ручными
прогонами (переживал только между production-запусками активного
воркфлоу — CLAUDE.md).

Anomalies_Log (недельный/дневной алерт-лог) в оригинале дублировался И в
Sheets, И в Postgres параллельными ветками — причём Telegram-алерт
(`Send a text message1`) в оригинале шёл ПОСЛЕ узла записи в Sheets, не
после узла записи в Postgres: если бы Sheets API споткнулся, алерт не ушёл
бы вообще, хотя строка в Postgres всё равно записалась бы (независимая
ветка) — реальная хрупкость, не нужная сегодня. По тому же решению, что
уже принято для `health.recommendations_log` (Weekly AI Advisor, #33,
одобрено без возражений) — здесь тоже оставлена ТОЛЬКО Postgres-запись
(health.anomaly_log, единственный источник, который реально читает
weekly_advisor.py), Sheets-копия Anomalies_Log СНЯТА. Убирает и хрупкость
(Telegram больше ни от чего не зависит), и лишний OAuth-вызов на каждый
детект. Если этот Sheets-лист всё же кому-то ещё нужен глазами — восстановить
несложно, скажи.

Digest_Log (недельный дайджест) раньше писался ТОЛЬКО в Google Sheets — на
момент решения выше заводить Postgres-таблицу ради того, что никто
программно не читает, было не по бюджету сложности. 2026-09-23 (постепенный
отказ от Sheets, категория A — Влад: "с нокодб я могу смотреть данные прямо
в постгре"): недостающая причина исчезла — теперь `health.digest_log`,
смотреть через NocoDB так же, как раньше смотрели в лист."""
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from app import anomaly_disposition
from app.db import get_conn
from app import notify
from app import run_log, timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
DAILY_HOUR_VL = 9
DAILY_MINUTE_VL = 15
WEEKLY_HOUR_VL = 11

HEALTH_DB_SHEET_ID = "1M8focgZBHCbhLEQb4GoyxTYxedA-FcdjQ_XakX5SG2w"
ANOMALY_ALERT_KEEP_DAYS = 3

_FALLBACK_METRICS = [
    {"key": "Чистый_сон_мин", "label": "Сон (чистый, мин)", "direction": "higher_better", "min_abs_delta": 30},
    {"key": "Эффективность_сна_", "label": "Эффективность сна", "direction": "higher_better", "is_percent": True, "min_abs_delta": 4},
    {"key": "Оценка_сна_балл", "label": "Оценка сна", "direction": "higher_better", "min_abs_delta": 7},
    {"key": "Пульс_ночной_средний", "label": "RHR ночной", "direction": "lower_better", "min_abs_delta": 3},
    {"key": "Восстановление_BodyBattery", "label": "Восстановление (BB)", "direction": "higher_better", "min_abs_delta": 12},
    {"key": "Стресс_дневной_средний", "label": "Стресс дневной", "direction": "lower_better", "min_abs_delta": 6},
    {"key": "VO2_Max", "label": "VO2 Max", "direction": "higher_better", "min_abs_delta": 2},
    {"key": "Питание_Всего_Ккал", "label": "Калории", "direction": "neutral", "min_abs_delta": 350},
    {"key": "Питание_Всего_Белки_г", "label": "Белок", "direction": "higher_better", "min_abs_delta": 25},
    {"key": "Шаги_за_вчера", "label": "Шаги", "direction": "neutral", "min_abs_delta": 3500},
    {"key": "ВСР_ночная", "label": "HRV ночная", "direction": "higher_better", "min_abs_delta": 6},
]

WINDOWS = [
    {"days": 7, "min_points": 4, "label": "7д"},
    {"days": 30, "min_points": 10, "label": "30д"},
    {"days": 90, "min_points": 20, "label": "90д"},
]

Z_MODERATE = 1.5
Z_STRONG = 2.0


def _to_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = str(v).strip().replace("%", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    s = str(v).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_metrics(cur) -> list[dict]:
    """Metric_Config (Google Sheets) с фолбэком на хардкод — общая для
    дневного/event и недельного путей (см. докстринг модуля)."""
    try:
        from app.sheets_client import get_values
        rows = get_values(HEALTH_DB_SHEET_ID, "Metric_Config")
    except Exception:
        logger.exception("anomaly_detector: Metric_Config недоступен, использую хардкод")
        return _FALLBACK_METRICS
    if not rows or len(rows) < 2:
        return _FALLBACK_METRICS
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    need = {"fkey", "col", "label", "direction", "min_abs_delta"}
    if not need.issubset(idx.keys()):
        return _FALLBACK_METRICS
    seen = set()
    out = []
    for r in rows[1:]:
        def g(k):
            i = idx.get(k)
            return r[i] if i is not None and i < len(r) else None
        direction = g("direction")
        col = g("col")
        if not direction or direction == "reference" or not col:
            continue
        fkey = g("fkey") or col
        if fkey in seen:
            continue
        seen.add(fkey)
        label = g("label") or col
        mad = _to_number(g("min_abs_delta")) or 0
        out.append({
            "key": col, "label": label, "direction": direction, "min_abs_delta": mad,
            "is_percent": bool(re.search(r"эффектив|%", str(label), re.I)),
        })
    return out or _FALLBACK_METRICS


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals)


def _std_sample(vals: list[float], m: float) -> Optional[float]:
    if len(vals) < 2:
        return None
    variance = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(variance)


def _get_baseline(records: list[dict], idx: int, metric_key: str, window_days: int, min_points: int) -> Optional[dict]:
    current_date = records[idx]["date"]
    frm = current_date - timedelta(days=window_days)
    values = [
        r["metrics"][metric_key]
        for r in records[:idx]
        if frm <= r["date"] < current_date and r["metrics"].get(metric_key) is not None
    ]
    if len(values) < min_points:
        return None
    m = _mean(values)
    sd = _std_sample(values, m)
    return {"mean": m, "std": sd, "count": len(values)}


def _classify(abs_z: Optional[float]) -> Optional[str]:
    if abs_z is None:
        return None
    if abs_z >= Z_STRONG:
        return "strong"
    if abs_z >= Z_MODERATE:
        return "moderate"
    return None


def detect_anomalies(daily_rows: list[dict], metrics: list[dict]) -> list[dict]:
    """Порт "Anomaly Detection"/"Anomaly Detection1" (идентичные, теперь
    одна функция). `daily_rows` — health.daily_trends (dict per день, ключ
    "Дата" — YYYY-MM-DD текст). Возвращает по одной записи на день, в
    хронологическом порядке, последняя — `is_latest=True`."""
    raw_records = []
    for j in daily_rows:
        d = _parse_date(j.get("Дата"))
        if not d:
            continue
        rec_metrics = {}
        for m in metrics:
            v = _to_number(j.get(m["key"]))
            if m.get("is_percent") and v is not None and v <= 1.5:
                v *= 100
            rec_metrics[m["key"]] = v
        raw_records.append({"date": d, "date_str": str(j["Дата"])[:10], "metrics": rec_metrics})
    raw_records.sort(key=lambda r: r["date"])

    output = []
    for idx, rec in enumerate(raw_records):
        anomalies = []
        for m in metrics:
            value = rec["metrics"].get(m["key"])
            if value is None:
                continue
            best_severity = best_z = best_baseline = best_window = None
            for w in WINDOWS:
                baseline = _get_baseline(raw_records, idx, m["key"], w["days"], w["min_points"])
                if not baseline or not baseline["std"]:
                    continue
                z = (value - baseline["mean"]) / baseline["std"]
                severity = _classify(abs(z))
                if severity and (not best_severity or (severity == "strong" and best_severity != "strong")):
                    best_severity, best_z, best_baseline, best_window = severity, z, baseline, w["label"]

            abs_delta = abs(value - best_baseline["mean"]) if best_baseline else 0
            passes_abs_gate = abs_delta >= (m.get("min_abs_delta") or 0)

            if best_severity and passes_abs_gate:
                direction = m["direction"]
                worse = (best_z < 0) if direction == "higher_better" else (best_z > 0) if direction == "lower_better" else None
                anomalies.append({
                    "metric": m["key"], "label": m["label"], "value": value,
                    "z": round(best_z, 2), "window": best_window, "severity": best_severity,
                    "baseline_mean": round(best_baseline["mean"], 2), "direction": direction,
                    "interpretation": "ухудшение" if worse is True else "улучшение" if worse is False else "отклонение",
                })
        output.append({
            "date": rec["date_str"], "anomalies": anomalies,
            "has_anomalies": len(anomalies) > 0, "is_latest": idx == len(raw_records) - 1,
        })
    return output


def _format_line(a: dict) -> str:
    arrow = "↑" if a["z"] > 0 else "↓"
    sev = "🔴 сильное" if a["severity"] == "strong" else "🟡 умеренное"
    return f"{sev} отклонение — {a['label']}: {a['value']} ({arrow} z={a['z']} за окно {a['window']}, baseline≈{a['baseline_mean']}) — {a['interpretation']}"


def _format_series_line(a: dict) -> str:
    """«Мост аномалия -> действие» (2026-09-25, Часть 1.2) — эскалированная
    серия (3 moderate за 7 дней) визуально отличается от одиночного strong-
    отклонения: честно об этом в тексте, не притворяется, что это тот же
    сильный z-score за один день."""
    arrow = "↑" if a["z"] > 0 else "↓"
    return (f"🟠 серия умеренных ({anomaly_disposition.SERIES_MIN_COUNT}× за "
            f"{anomaly_disposition.SERIES_WINDOW_DAYS}д) — {a['label']}: {a['value']} "
            f"({arrow} z={a['z']} за окно {a['window']}, baseline≈{a['baseline_mean']}) — {a['interpretation']}")


# =====================================================================
# Извлечение последнего дня + алерт + запись (дневной/event путь)
# =====================================================================

def _sort_anomalies(anomalies: list[dict]) -> list[dict]:
    rank = {"strong": 0, "moderate": 1}
    return sorted(anomalies, key=lambda a: rank.get(a["severity"], 9))


def _prune_alerted(cur) -> None:
    cur.execute(
        "DELETE FROM health.anomaly_alerted WHERE alert_date < %s",
        (date.today() - timedelta(days=ANOMALY_ALERT_KEEP_DAYS),),
    )


def _already_alerted(cur, day: str, label: str) -> bool:
    cur.execute("SELECT 1 FROM health.anomaly_alerted WHERE key = %s", (f"{day}|{label}",))
    return cur.fetchone() is not None


def _mark_alerted(cur, day: str, label: str) -> None:
    cur.execute(
        "INSERT INTO health.anomaly_alerted (key, alert_date) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (f"{day}|{label}", day),
    )


def mark_daily_check_ran(cur, day: str) -> None:
    """2026-09-22 (реальный ложный алерт system_check.py, найдено по репорту
    Влада): отмечает, что дневная проверка РЕАЛЬНО исполнилась для этого дня —
    независимо от того, нашла ли она аномалии. write_anomaly_log() ниже
    пишет строку в health.anomaly_log ТОЛЬКО когда есть находки — без этой
    отдельной отметки нельзя отличить "аномалий не было" (законно ничего не
    записано) от "детектор вообще не запускался". card.anomaly_detector_state,
    не health.* — это операционное состояние процесса, не медданные."""
    cur.execute(
        "INSERT INTO card.anomaly_detector_state (id, last_run_at, last_day_checked) "
        "VALUES (1, now(), %s::date) "
        "ON CONFLICT (id) DO UPDATE SET last_run_at = now(), last_day_checked = EXCLUDED.last_day_checked",
        (day,),
    )


def write_anomaly_log(cur, day: str, anomalies: list[dict]) -> None:
    """health.anomaly_log — то же, что "Anomaly_log (Postgres)" в оригинале.
    Пишется НЕЗАВИСИМО от Telegram (см. докстринг модуля — в оригинале
    Telegram шёл только после успеха Sheets-ветки, здесь обе стороны
    независимы по конструкции)."""
    sorted_a = _sort_anomalies(anomalies)
    cur.execute(
        """INSERT INTO health.anomaly_log (date, anomaly_count, strong_count, raw_anomalies)
           VALUES (%s::date, %s, %s, %s::jsonb)
           ON CONFLICT (date) DO UPDATE SET
             anomaly_count = EXCLUDED.anomaly_count, strong_count = EXCLUDED.strong_count,
             raw_anomalies = EXCLUDED.raw_anomalies, created_at = now()""",
        (day, len(sorted_a), sum(1 for a in sorted_a if a["severity"] == "strong"), json.dumps(sorted_a, ensure_ascii=False)),
    )


def _fetch_daily_and_metrics(cur) -> tuple[list[dict], list[dict]]:
    cur.execute('SELECT d.*, to_char(d."Дата", \'YYYY-MM-DD\') AS "Дата" FROM health.daily_trends d ORDER BY d."Дата"')
    cols = [c.name for c in cur.description]
    daily_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return daily_rows, _load_metrics(cur)


def run_daily_check() -> None:
    """Порт связки "Anomaly Detection" -> "Extract Latest Day" -> "If" ->
    "Format Alert Text" -> "Prepare Anomalies Rows" -> Anomaly_log (Postgres)
    + Telegram. Вызывается из трёх мест — см. докстринг модуля."""
    with get_conn() as conn, conn.cursor() as cur:
        daily_rows, metrics = _fetch_daily_and_metrics(cur)

    days = detect_anomalies(daily_rows, metrics)
    latest = next((d for d in days if d["is_latest"]), None)
    if latest:
        with get_conn() as conn, conn.cursor() as cur:
            mark_daily_check_ran(cur, latest["date"])
            conn.commit()
    if not latest or not latest["has_anomalies"]:
        return

    day = latest["date"]
    sorted_a = _sort_anomalies(latest["anomalies"])

    with get_conn() as conn, conn.cursor() as cur:
        write_anomaly_log(cur, day, sorted_a)
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        _prune_alerted(cur)
        unsent = [a for a in sorted_a if not _already_alerted(cur, day, a["label"])]
        if unsent:
            for a in unsent:
                _mark_alerted(cur, day, a["label"])
        conn.commit()

    if not unsent:
        return  # все аномалии за этот день уже отправлялись — молча не дублируем

    # ROADMAP 5.1 (2026-09-24): сильные (strong) — critical, немедленно (в
    # рамках дневного бюджета); умеренные (moderate, "жёлтые") — normal, в
    # вечерний дайджест. Раньше слались ОДНИМ сообщением независимо от
    # состава — жёлтая находка была так же громкой, как сильная.
    strong = [a for a in unsent if a["severity"] == "strong"]
    moderate = [a for a in unsent if a["severity"] != "strong"]

    # «Мост аномалия -> действие» (2026-09-25, G5 VISION, Часть 1.2): серия
    # 3 moderate по одной метрике за 7 дней эскалирует до strong-логики (сигнал,
    # не шум) — читает то, что детектор уже записал в health.anomaly_log,
    # z-score/baseline-математику детектора не трогает (граница тикета).
    escalated, still_moderate = [], []
    with get_conn() as conn, conn.cursor() as cur:
        for a in moderate:
            count = anomaly_disposition.check_series_escalation(cur, a["metric"], day)
            (escalated if count >= anomaly_disposition.SERIES_MIN_COUNT else still_moderate).append(a)
    moderate = still_moderate

    # У каждой strong/эскалированной — судьба, не просто сообщение (Часть 1.1):
    # активное suppress глушит critical для ЭТОЙ метрики (но пишет строку —
    # "видимая тишина, не слепота", Часть 3.1), иначе — pending + алерт с
    # подсказкой-ответом.
    alertable = []
    with get_conn() as conn, conn.cursor() as cur:
        for a, sev in [(a, "strong") for a in strong] + [(a, "moderate_series") for a in escalated]:
            suppressed = anomaly_disposition.active_suppression(cur, a["metric"], day)
            if suppressed:
                anomaly_disposition.create_disposition_row(
                    cur, a["metric"], a["label"], day, sev,
                    disposition="suppress", reason=suppressed["reason"], disposed_by="system",
                )
            else:
                anomaly_disposition.create_disposition_row(cur, a["metric"], a["label"], day, sev)
                alertable.append((a, sev))
        conn.commit()

    if alertable:
        lines = [_format_series_line(a) if sev == "moderate_series" else _format_line(a) for a, sev in alertable]
        notify.notify("anomaly_detector", "critical",
                      f"🚨 Обнаружены аномалии за {day}\n\n" + "\n".join(lines) + f"\n\n{anomaly_disposition.REPLY_HINT}")
    if moderate:
        lines = [_format_line(a) for a in moderate]
        notify.notify("anomaly_detector", "normal", f"🟡 Умеренные отклонения за {day}\n\n" + "\n".join(lines))


# =====================================================================
# Недельный дайджест (воскресенье 11:00 ВЛ)
# =====================================================================

def build_weekly_digest(days: list[dict]) -> Optional[dict]:
    """Порт "Weekly Slice" + "Format Weekly Digest". `days` — полный вывод
    detect_anomalies (все дни истории)."""
    if not days:
        return None
    all_sorted = sorted(days, key=lambda d: d["date"])
    last_date = _parse_date(all_sorted[-1]["date"])
    week_ago = last_date - timedelta(days=7)
    week = [d for d in all_sorted if week_ago < _parse_date(d["date"]) <= last_date]
    if not week:
        return None

    all_anomalies = [dict(a, date=d["date"]) for d in week for a in d["anomalies"]]
    by_metric: dict = {}
    for a in all_anomalies:
        e = by_metric.setdefault(a["metric"], {"label": a["label"], "occurrences": 0, "days": []})
        e["occurrences"] += 1
        e["days"].append(a["date"])
    metric_summary_lines = [f"{m['label']}: аномалия {m['occurrences']}× за неделю ({', '.join(m['days'])})" for m in by_metric.values()]

    return {
        "period_start": week[0]["date"], "period_end": week[-1]["date"],
        "days_with_data": len(week), "total_anomalies": len(all_anomalies),
        "strong_anomalies": sum(1 for a in all_anomalies if a["severity"] == "strong"),
        "metric_summary_lines": metric_summary_lines,
        "days_detail": [
            {"date": d["date"], "anomaly_count": len(d["anomalies"]),
             "anomalies": [f"{a['label']} (z={a['z']}, {a['severity']})" for a in d["anomalies"]]}
            for d in week
        ],
    }


def _append_digest_row(d: dict) -> None:
    """2026-09-23 (постепенный отказ от Sheets, категория A): было append_row
    в Google Sheets — теперь UPSERT в health.digest_log (period_start — PK,
    идемпотентно на случай повторного запуска той же недели)."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO health.digest_log "
                "(period_start, period_end, days_with_data, total_anomalies, strong_anomalies, "
                "metric_summary, days_detail) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (period_start) DO UPDATE SET "
                "period_end = EXCLUDED.period_end, days_with_data = EXCLUDED.days_with_data, "
                "total_anomalies = EXCLUDED.total_anomalies, strong_anomalies = EXCLUDED.strong_anomalies, "
                "metric_summary = EXCLUDED.metric_summary, days_detail = EXCLUDED.days_detail",
                (d["period_start"], d["period_end"], d["days_with_data"], d["total_anomalies"],
                 d["strong_anomalies"], " | ".join(d["metric_summary_lines"]),
                 json.dumps(d["days_detail"], ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        logger.exception("anomaly_detector: не удалось записать health.digest_log (не блокирует Telegram)")


def run_weekly_digest() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        daily_rows, metrics = _fetch_daily_and_metrics(cur)

    days = detect_anomalies(daily_rows, metrics)
    digest = build_weekly_digest(days)
    if not digest:
        return

    _append_digest_row(digest)

    lines = "\n".join(digest["metric_summary_lines"])
    notify.notify(
        "anomaly_detector_weekly", "normal",
        f"📊 Недельный дайджест ({digest['period_start']} — {digest['period_end']})\n"
        f"Дней с данными: {digest['days_with_data']}\n"
        f"Всего аномалий: {digest['total_anomalies']} (сильных: {digest['strong_anomalies']})\n\n{lines}",
    )


# =====================================================================
# Планировщики
# =====================================================================

def _sleep_until(hour: int, minute: int = 0, weekday: Optional[int] = None) -> None:
    """Фаза 3 (2026-09-22): сон до часа ПО ПОЯСУ ЧЕЛОВЕКА (timeutil), кусками
    по 10 минут — переключение /tz подхватывается без ожидания следующего дня."""
    timeutil.sleep_until_local(hour, minute, weekday=weekday)


def run_daily_maintenance() -> None:
    """«Мост аномалия -> действие» (2026-09-25) — обслуживание диспозиций,
    ТОЛЬКО из run_daily_scheduler() (не из run_daily_check(), которую зовут
    ещё и после каждого ингеста/из недельного пути — иначе строка в дайджест
    могла бы уйти чаще раза в день, Часть 1.4 явно это запрещает).

    1. Часть 4.1: расследование, открытое из аномалии, закрылось где-то в
       обычном разговоре с доктором — синхронизируем disposition='explained'.
    2. Часть 1.4: pending старше суток — ОДНА строка в вечерний дайджест
       (notify priority=normal — тот же общий дайджест, не отдельное
       сообщение и не новый канал)."""
    with get_conn() as conn, conn.cursor() as cur:
        explained = anomaly_disposition.sync_resolved_investigations(cur)
        pending = anomaly_disposition.pending_older_than(cur, hours=24)
        conn.commit()
    if explained:
        logger.info("anomaly_disposition: %d аномалий помечено explained (расследование закрыто)", explained)
    line = anomaly_disposition.format_pending_digest_line(pending)
    if line:
        notify.notify("anomaly_pending_reminder", "normal", line)


def run_daily_scheduler() -> None:
    logger.info("anomaly_detector daily scheduler: старт (%02d:%02d ВЛ)", DAILY_HOUR_VL, DAILY_MINUTE_VL)
    while True:
        try:
            _sleep_until(DAILY_HOUR_VL, DAILY_MINUTE_VL)
            run_daily_check()
            run_daily_maintenance()
            run_log.mark_run("anomaly_detector_daily")
        except Exception as e:
            logger.exception("anomaly_detector: run_daily_check упал — повтор завтра")
            alert_on_failure("anomaly_detector_daily", e)
            time.sleep(3600)


def run_weekly_scheduler() -> None:
    logger.info("anomaly_detector weekly scheduler: старт (вс %02d:00 ВЛ)", WEEKLY_HOUR_VL)
    while True:
        try:
            _sleep_until(WEEKLY_HOUR_VL, 0, weekday=6)  # 6 = воскресенье (Python Monday=0)
            run_weekly_digest()
            run_log.mark_run("anomaly_detector_weekly")
        except Exception as e:
            logger.exception("anomaly_detector: run_weekly_digest упал — повтор через неделю")
            alert_on_failure("anomaly_detector_weekly", e)
            time.sleep(3600)
