"""Порт n8n `Monthly_Trend_Wellness` (2026-09-21, группа 1, предпоследний
пункт — 16 нод, 3 отдельных Google Sheets писателя). Раз в месяц (1-е число,
10:00 ВЛ) сравнивает только что закончившийся месяц с предыдущим по питанию
(health.day_sum) и велнесу (health.daily_trends): суммы/средние за месяц +
значимые сдвиги (независимый двухвыборочный z-тест, |z|>=1.5, минимум 10
валидных точек в каждом месяце).

Оригинал писал в 3 Google Sheets (month_sum, Month_Wellness_Log,
Nutrinion_Monthly_Trend_Log) — ничего не читает их обратно (проверено: ни
один другой n8n-воркфлоу, ни card-service), тот же прецедент, что уже принят
для health.recommendations_log (#33) и Anomalies_Log (#34). Порт пишет в три
JSONB/строчные таблицы Postgres вместо них.

НАЙДЕНО при переносе: у "Nutrinion_Monthly_Trend_Log" схема Sheets была
настроена под СТАРЫЕ имена полей (prev_month_avg/this_month_avg/pct_change/
period_month), а код "Compute Trend v/v2" уже давно отдаёт ДРУГИЕ имена
(prev_month_mean/this_month_mean/z/n_current/n_previous) — проверил живой
лист: n8n autoMapInputData сам дописал новые колонки в шапку, данные не
терялись, но старые колонки висели вечно пустыми мёртвым грузом. Порт
использует только реальные (актуально пишущиеся) имена полей — не
перетаскиваю мёртвые.

Оригинал также дублировал ОДИН И ТОТ ЖЕ код дважды под разными именами узлов
("Split & Tag — Nutrition"/"— Wellness", "Aggregate Month — Nutrition"/
"— Wellness", "Compute Trend v"/"v2") — отличие между копиями было только в
constants DOMAIN/DATE_FIELD внутри каждой. Порт — один набор функций,
параметризованных, а не пять пар близнецов."""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

VL = timezone(timedelta(hours=10))
MONTHLY_HOUR_VL = 10
CHAT_ID = "8956401"
Z_MODERATE = 1.5
Z_STRONG = 2.0
MIN_N = 10
_EXCLUDE_FIELDS = {"User_ID", "Date", "Дата", "__period", "domain", "row_number"}

# Порт DIRECTION 1:1 из "Compute Trend v"/"v2" — намеренно СВОЙ список, не
# app.anomaly_detector's METRICS: разная статистика (двухвыборочный z-тест
# месяц-к-месяцу, а не отклонение от скользящей базы) и частично другой
# набор метрик (шире по питанию), унифицировать с anomaly_detector — не по
# бюджету сложности при разной механике под капотом.
DIRECTION = {
    "Calories": "neutral", "Proteins": "higher_better", "Carbs": "neutral", "Fats": "neutral",
    "Магний": "higher_better", "Витамин D": "higher_better", "Омега-3 (EPA/DHA)": "higher_better",
    "Клетчатка": "higher_better", "Холестерин": "lower_better", "Добавленный сахар": "lower_better",
    "Натрий": "lower_better", "Насыщенные жиры": "lower_better", "Трансжиры": "lower_better",
    "Пульс_ночной_средний": "lower_better", "ВСР_ночная": "higher_better",
    "Оценка_сна_балл": "higher_better", "Стресс_дневной_средний": "lower_better",
    "Восстановление_BodyBattery": "higher_better", "VO2_Max": "higher_better",
    "Шаги_за_вчера": "higher_better", "Эффективность_сна_": "higher_better",
}


def _to_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def define_month_windows(now: Optional[datetime] = None) -> dict:
    """Порт "Define Month Windows" — на 1-е число месяца current_month это
    только что закончившийся месяц, previous_month — тот, что перед ним."""
    now = now or datetime.now(VL)
    current = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    prev_anchor = now.replace(day=1) - timedelta(days=1)
    previous = (prev_anchor.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {"current_month": current, "previous_month": previous}


def split_and_tag(rows: list[dict], date_field: str, domain: str, windows: dict) -> list[dict]:
    """Порт "Split & Tag — Nutrition"/"— Wellness" (были байт-в-байт
    одинаковым кодом с разными константами) — один параметризованный проход."""
    tagged = []
    for r in rows:
        date_val = str(r.get(date_field) or "")[:7]
        if date_val == windows["current_month"]:
            tagged.append({**r, "__period": "current", "domain": domain})
        elif date_val == windows["previous_month"]:
            tagged.append({**r, "__period": "previous", "domain": domain})
    return tagged


def aggregate_month(rows: list[dict], month_key: str) -> Optional[tuple[dict, dict]]:
    """Порт "Aggregate Month — Nutrition"/"— Wellness" (тоже были близнецами).
    Возвращает (total_row, avg_row) или None, если за текущий месяц нет строк
    (в оригинале в этом случае писалась строка-предупреждение — здесь просто
    ничего не пишем, эффект тот же: нет полноценной записи за месяц)."""
    current = [r for r in rows if r.get("__period") == "current"]
    if not current:
        return None

    field_set = set()
    for r in current:
        for k in r.keys():
            if k not in _EXCLUDE_FIELDS:
                field_set.add(k)

    sums: dict = {f: 0.0 for f in field_set}
    counts: dict = {f: 0 for f in field_set}
    for r in current:
        for f in field_set:
            v = _to_number(r.get(f))
            if v is not None:
                sums[f] += v
                counts[f] += 1

    total_row = {"User_ID": "Влад Васюк", "Date": month_key, "Способ подсчета": "total"}
    avg_row = {"User_ID": "Влад Васюк", "Date": month_key, "Способ подсчета": "average"}
    for f in field_set:
        total_row[f] = round(sums[f], 2)
        avg_row[f] = round(sums[f] / counts[f], 2) if counts[f] > 0 else None
    return total_row, avg_row


def _mean_std(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"mean": None, "std": None, "n": 0}
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1 if n > 1 else 1)
    return {"mean": mean, "std": variance ** 0.5, "n": n}


def compute_trend(rows: list[dict]) -> list[dict]:
    """Порт "Compute Trend v"/"v2" (тоже были близнецами) — двухвыборочный
    z-тест (Уэлч-подобный SE) месяц-к-месяцу, |z|>=1.5, минимум MIN_N точек
    в каждом месяце."""
    if not rows:
        return []
    domain = rows[0].get("domain", "unknown")
    current_rows = [r for r in rows if r.get("__period") == "current"]
    prev_rows = [r for r in rows if r.get("__period") == "previous"]

    field_set = set()
    for r in current_rows:
        for k in r.keys():
            if k not in _EXCLUDE_FIELDS:
                field_set.add(k)

    results = []
    for m in field_set:
        cur_vals = [v for v in (_to_number(r.get(m)) for r in current_rows) if v is not None]
        prev_vals = [v for v in (_to_number(r.get(m)) for r in prev_rows) if v is not None]
        if len(cur_vals) < MIN_N or len(prev_vals) < MIN_N:
            continue

        cur, prev = _mean_std(cur_vals), _mean_std(prev_vals)
        se = ((cur["std"] ** 2) / cur["n"] + (prev["std"] ** 2) / prev["n"]) ** 0.5
        if se == 0:
            continue
        z = (cur["mean"] - prev["mean"]) / se
        abs_z = abs(z)

        severity = "strong" if abs_z >= Z_STRONG else "moderate" if abs_z >= Z_MODERATE else None
        if not severity:
            continue

        direction = DIRECTION.get(m, "neutral")
        if direction == "higher_better":
            interpretation = "улучшение" if z > 0 else "ухудшение"
        elif direction == "lower_better":
            interpretation = "ухудшение" if z > 0 else "улучшение"
        else:
            interpretation = "изменение"

        results.append({
            "date_computed": datetime.now(VL).date().isoformat(), "domain": domain, "metric": m,
            "prev_month_mean": round(prev["mean"], 2), "this_month_mean": round(cur["mean"], 2),
            "z": round(z, 2), "n_current": cur["n"], "n_previous": prev["n"],
            "severity": severity, "direction": direction, "interpretation": interpretation,
        })
    return results


def write_month_sum(cur, month_key: str, total_row: dict, avg_row: dict) -> None:
    for calc_method, row in (("total", total_row), ("average", avg_row)):
        metrics = {k: v for k, v in row.items() if k not in ("User_ID", "Date", "Способ подсчета")}
        cur.execute(
            """INSERT INTO health.month_sum (month, calc_method, metrics) VALUES (%s, %s, %s::jsonb)
               ON CONFLICT (month, calc_method) DO UPDATE SET metrics = EXCLUDED.metrics, computed_at = now()""",
            (month_key, calc_method, json.dumps(metrics, ensure_ascii=False)),
        )


def write_month_wellness_log(cur, month_key: str, total_row: dict, avg_row: dict) -> None:
    for calc_method, row in (("total", total_row), ("average", avg_row)):
        metrics = {k: v for k, v in row.items() if k not in ("User_ID", "Date", "Способ подсчета")}
        cur.execute(
            """INSERT INTO health.month_wellness_log (month, calc_method, metrics) VALUES (%s, %s, %s::jsonb)
               ON CONFLICT (month, calc_method) DO UPDATE SET metrics = EXCLUDED.metrics, computed_at = now()""",
            (month_key, calc_method, json.dumps(metrics, ensure_ascii=False)),
        )


def write_monthly_trend_log(cur, period_month: str, trends: list[dict]) -> None:
    for t in trends:
        cur.execute(
            """INSERT INTO health.monthly_trend_log
               (date_computed, period_month, domain, metric, prev_month_mean, this_month_mean,
                z, n_current, n_previous, severity, direction, interpretation)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (period_month, domain, metric) DO UPDATE SET
                 prev_month_mean = EXCLUDED.prev_month_mean, this_month_mean = EXCLUDED.this_month_mean,
                 z = EXCLUDED.z, n_current = EXCLUDED.n_current, n_previous = EXCLUDED.n_previous,
                 severity = EXCLUDED.severity, direction = EXCLUDED.direction,
                 interpretation = EXCLUDED.interpretation, computed_at = now()""",
            (t["date_computed"], period_month, t["domain"], t["metric"], t["prev_month_mean"],
             t["this_month_mean"], t["z"], t["n_current"], t["n_previous"], t["severity"],
             t["direction"], t["interpretation"]),
        )


def build_telegram_text(period_month: str, trends: list[dict]) -> str:
    """Порт "Send a text message" — тот же формат текста, что в оригинале."""
    strong = [t for t in trends if t["severity"] == "strong"]
    top = (strong if strong else trends)[:6]
    lines = []
    for t in top:
        arrow = "📈" if t["interpretation"] == "улучшение" else "📉" if t["interpretation"] == "ухудшение" else "↔️"
        lines.append(f"{arrow} {t['metric']} ({t['domain']}): {t['prev_month_mean']} → {t['this_month_mean']}")
    return (
        f"📊 Месячный тренд за {period_month}\n"
        f"Сдвигов: {len(trends)} (сильных: {len(strong)})\n\n" + "\n".join(lines)
    )


def run_once() -> None:
    from app.db import get_conn
    from app import hermes_telegram as telegram  # 2026-09-21: алерты -> Hermes (см. app/hermes_telegram.py)

    windows = define_month_windows()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT d.*, to_char(d."Date", \'YYYY-MM-DD\') AS "Date" FROM health.day_sum d ORDER BY d."Date"')
        cols = [c.name for c in cur.description]
        day_sum_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute('SELECT d.*, to_char(d."Дата", \'YYYY-MM-DD\') AS "Дата" FROM health.daily_trends d ORDER BY d."Дата"')
        cols = [c.name for c in cur.description]
        daily_trends_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    nutrition_tagged = split_and_tag(day_sum_rows, "Date", "nutrition", windows)
    wellness_tagged = split_and_tag(daily_trends_rows, "Дата", "wellness", windows)

    nutrition_agg = aggregate_month(nutrition_tagged, windows["current_month"])
    wellness_agg = aggregate_month(wellness_tagged, windows["current_month"])

    nutrition_trend = compute_trend(nutrition_tagged)
    wellness_trend = compute_trend(wellness_tagged)
    all_trends = nutrition_trend + wellness_trend

    with get_conn() as conn, conn.cursor() as cur:
        if nutrition_agg:
            write_month_sum(cur, windows["current_month"], *nutrition_agg)
        else:
            logger.warning("monthly_trend: нет строк day_sum за %s, month_sum не записан", windows["current_month"])
        if wellness_agg:
            write_month_wellness_log(cur, windows["current_month"], *wellness_agg)
        else:
            logger.warning("monthly_trend: нет строк daily_trends за %s, month_wellness_log не записан", windows["current_month"])
        if all_trends:
            write_monthly_trend_log(cur, windows["current_month"], all_trends)
        conn.commit()

    if all_trends:
        telegram.send_message(CHAT_ID, build_telegram_text(windows["current_month"], all_trends))
    logger.info("monthly_trend: готово за %s, сдвигов: %d", windows["current_month"], len(all_trends))


def _sleep_until_first_of_month(hour: int) -> None:
    now = datetime.now(VL)
    if now.day == 1 and now.hour < hour:
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    else:
        # следующее 1-е число следующего месяца
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        nxt = now.replace(year=year, month=month, day=1, hour=hour, minute=0, second=0, microsecond=0)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def run_scheduler() -> None:
    logger.info("monthly_trend scheduler: старт (1-е число, %02d:00 ВЛ)", MONTHLY_HOUR_VL)
    while True:
        try:
            _sleep_until_first_of_month(MONTHLY_HOUR_VL)
            run_once()
        except Exception as e:
            logger.exception("monthly_trend: run_once упал — повтор через сутки")
            alert_on_failure("monthly_trend", e)
            time.sleep(3600)
