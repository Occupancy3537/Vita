"""Порт n8n `Collect_Biohacking_Data` (2026-09-20, группа 3, вторая половина
— переносится вместе с app/anomaly_detector.py, см. его докстринг про
причину совместного переноса).

Это ровно тот вебхук, на который `send_to_n8n.py` (garminbot,
~/.garmin-givemydata/) шлёт готовую выборку из своей SQLite каждую ночь.
Решение Влада 2026-09-20: вместо публичного URL через nginx/n8n —
`send_to_n8n.py`'s WEBHOOK_URL правится на прямой localhost card-service
(`http://127.0.0.1:8080/ingest/biohacking`, порт слушает 127.0.0.1 — garminbot
и card-service на одном VPS, публичный маршрут через nip.io не нужен и не
заводился — тот же принцип, что у /ingest и остальных пишущих эндпоинтов
в main.py, см. их докстринг).

Пайплайн: Гармин (тело запроса) + питание (health.meals, 10 дней) + климат
спальни (Google Sheets MicroClimate — ПОЧАСОВОЙ, не дневной агрегат:
health.microclimate из #28 хранит один агрегат в СУТКИ и не годится для
усреднения строго по часам сна, поэтому здесь живой Sheets-запрос, тот же
принцип, что и Metric_Config в anomaly_detector.py) + календарь (Google
Calendar, лекарства по ключевым словам в названиях событий) + RescueTime
(экранное время) + погода (Open-Meteo, без ключа) → одна строка
health.daily_trends (динамический UPSERT, только присутствующие поля —
"Дата" НИКОГДА не входит в исключаемые, остальное как решил "Code in
JavaScript") + синхрон части метрик в card-service facts (теперь ПРЯМОЙ
вызов в процессе, а не HTTP POST на самого себя, каким он был у n8n) +
вызов anomaly_detector. Дубль в Google Sheets (sync_to_sheets) — СНЯТ
2026-09-23 (постепенный отказ от Sheets, категория A): health.daily_trends
и так был единственным каноном, Sheets был чистым зеркалом для глаз — NocoDB
закрывает ту же потребность прямо на Postgres.

НАЙДЕНО при переносе, реальный баг, не нужно чинить отдельно — порт САМ
его устраняет: "Write DT PG" в оригинале падал 11 и 12 сентября с
"Query Parameters must be a string of comma-separated values or an array
of values" (execution_entity id 17933/18924) — это баг n8n-ноды Postgres v2
на многострочных значениях (Лекарства_принимаемые с несколькими
препаратами через \\n в одном параметре). psycopg с обычными
параметризованными запросами такой проблемы структурно не имеет.
Данные не терялись насовсем — Sheets-ветка независима и пишет всегда,
ночной sheets_to_pg_mirror.js подтягивает — но прямая запись в тот
момент молчала.

RescueTime API-ключ раньше лежал открытым текстом в параметрах httpRequest-
ноды — здесь вынесен в переменную окружения RESCUETIME_API_KEY (гигиена,
не поведенческое отличие)."""
import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from app import timeutil
from app.db import get_conn

logger = logging.getLogger(__name__)

MICROCLIMATE_SHEET_ID = "1wejYO7GKnizGQ9QIMzPua4NxY8uGKwRIcJxBDvyfcqw"
MICROCLIMATE_RANGE = "Лист1"
CALENDAR_ID = "vasukvladislav@gmail.com"
WEATHER_LAT, WEATHER_LON = "43.11", "131.88"

KNOWN_COLS = [
    "Дата", "Время_отбоя", "Время_подъема", "Время_в_кровати_мин", "Чистый_сон_мин",
    "Эффективность_сна_", "Глубокий_сон_мин", "Глубокий_1_половина_мин", "Глубокий_2_половина_мин",
    "REM_сон_мин", "Легкий_сон_мин", "Пробуждения_кол_во", "Бодрствование_мин",
    "Пульс_ночной_средний", "Пульс_ночной_мин", "Пульс_ночной_макс", "Оценка_сна_балл",
    "Беспокойные_моменты", "Дыхание_ночь_среднее", "SpO2_ночь_среднее", "Температура_avg_C",
    "Влажность_avg_%", "PM25_avg", "Ужин_время", "Завтрак_время", "Окно_голода_до_сна_ч",
    "Длительность_голода_ч", "Ужин_Ккал", "Ужин_Белки_г", "Ужин_Жиры_г", "Ужин_Углеводы_г",
    "Шаги_за_вчера", "Тренировка_Ккал", "Тренировка_поздняя", "Атмосферное_давление_ночь_hPa",
    "Лекарства_принимаемые", "Время_последнего_кофе", "Алкоголь_гр", "Жалобы_вчера",
    "Пометки", "Дефициты", "Экранное_время_всего_ч", "Экран_Продуктивно_ч", "Экран_Отвлечения_ч",
    "Восстановление_BodyBattery", "Стресс_дневной_средний", "VO2_Max", "Дыхание_тип",
    "Дыхание_мин", "Экран_перед_сном_мин", "ВСР_ночная", "Питание_Всего_Ккал",
    "Питание_Всего_Белки_г", "Питание_Всего_Жиры_г", "Питание_Всего_Углеводы_г",
    "Тренировка_1_Тип", "Тренировка_1_Мин", "Тренировка_2_Тип", "Тренировка_2_Мин",
    "Тренировка_3_Тип", "Тренировка_3_Мин", "Темп_тренировки_avg_C", "Темп_тренировки_min_C",
    "Темп_тренировки_max_C", "Training_Status", "Training_Acute_Load", "Training_Chronic_Load",
    "Точка_росы_avg_C", "Атм_давление_Дельта_12ч", "Атм_давление_Дельта_24ч",
    "Освещенность_ч_сутки", "Индекс_когнитивной_нагрузки", "ACWR_Garmin", "ACWR_Status",
    "Garmin_устройство", "Провал_без_движения_мин", "Плавание_было",
]

# 2026-09-23 (по запросу Влада): порог "день прошёл правильно" по движению —
# калиброван на 117 днях реальной истории (94/117 при 40 мин; последние 14
# дней — 71%, значит достижимо, не генератор тревоги на пустом месте). Не
# хранится отдельным bool-столбцом ("День_движения_ок") — храним только сырое
# число (минуты самого длинного провала), порог живёт здесь и на дашборде,
# чтобы менять его в одном месте, не гоняясь за уже записанными строками.
MOVEMENT_GAP_OK_THRESHOLD_MIN = 40
MOVEMENT_DAY_START_HOUR = 7   # 2026-09-23: фиксированное окно "обычно не сплю" —
MOVEMENT_DAY_END_HOUR = 23    # сознательное упрощение v1, не берём вчерашний
                              # подъём/сегодняшний отбой (см. докстринг ниже)


def longest_sedentary_gap_minutes(
    movement_minutes: Optional[list], threshold: float = 0.3,
    day_start_hour: int = MOVEMENT_DAY_START_HOUR, day_end_hour: int = MOVEMENT_DAY_END_HOUR,
) -> Optional[float]:
    """Самый длинный НЕПРЕРЫВНЫЙ провал без движения (минуты) за день, только
    внутри окна [day_start_hour, day_end_hour) по местному времени.

    2026-09-23, ОСОЗНАННОЕ упрощение v1: правильнее было бы брать окно
    бодрствования по факту (от вчерашнего подъёма до сегодняшнего отбоя), но
    вчерашний подъём — это запись ПРЕДЫДУЩЕГО дня, сюда не долетает без
    отдельного запроса к health.daily_trends. Фиксированное окно 07:00-23:00
    откалибровано на 117 днях реальной истории Влада (см. AGENT_SYNC) и даёт
    разумный результат — не идеально, но лучше, чем считать провалы во сне
    как "не двигался"."""
    if not movement_minutes:
        return None
    pts = sorted((m for m in movement_minutes if day_start_hour * 60 <= m[0] < day_end_hour * 60),
                 key=lambda m: m[0])
    if not pts:
        return None
    max_gap = 0.0
    gap_start = None
    prev_minute = pts[0][0]
    for minute, val in pts:
        if val <= threshold:
            if gap_start is None:
                gap_start = minute
        else:
            if gap_start is not None:
                max_gap = max(max_gap, minute - gap_start)
                gap_start = None
        prev_minute = minute
    if gap_start is not None:
        max_gap = max(max_gap, prev_minute - gap_start)
    return round(max_gap)

# app.nutrition_reports.py / app.food_diary.py уже используют _js_round для того
# же самого JS Math.round-vs-Python-round расхождения (round-half-up vs banker's
# rounding) — здесь используется тот же приём, инлайн (модуль не импортирует
# dashboard.py, чтобы не тянуть его LLM/дашборд-зависимости ради одной функции).
def _js_round1(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return math.floor(x * 10 + 0.5) / 10


class BiohackingPayload(BaseModel):
    """Ровно то, что шлёт send_to_n8n.py (payload — плоский dict, без
    обёртки). Все поля Optional — оригинал тоже терпим к пропускам
    (`garmin.foo || null` на каждое поле)."""
    date: str
    bedtime: Optional[str] = None
    wakeup_time: Optional[str] = None
    sleep_total_min: Optional[float] = None
    sleep_deep_min: Optional[float] = None
    sleep_rem_min: Optional[float] = None
    awake_count: Optional[float] = None
    awake_time_min: Optional[float] = None
    sleep_score: Optional[float] = None
    respiration_avg: Optional[float] = None
    respiration_min: Optional[float] = None
    spo2_avg: Optional[float] = None
    body_battery_recharge: Optional[float] = None
    restless_moments: Optional[float] = None
    hrv_night: Optional[float] = None
    hr_night_avg: Optional[float] = None
    hr_night_min: Optional[float] = None
    hr_night_max: Optional[float] = None
    hr_day_avg: Optional[float] = None
    hr_day_max: Optional[float] = None
    day_stress_avg: Optional[float] = None
    day_stress_max: Optional[float] = None
    steps: Optional[float] = None
    vo2max: Optional[float] = None
    workouts_raw: Optional[str] = None
    breathwork_min: Optional[float] = None
    breathwork_type: Optional[str] = None
    late_workout_flag: Optional[bool] = None
    temp_activity_avg_c: Optional[float] = None
    temp_activity_min_c: Optional[float] = None
    temp_activity_max_c: Optional[float] = None
    training_status: Optional[str] = None
    training_acute_load: Optional[float] = None
    training_chronic_load: Optional[float] = None
    acwr_garmin: Optional[float] = None
    acwr_status: Optional[str] = None
    garmin_device: Optional[str] = None
    deep1_min: Optional[float] = None
    deep2_min: Optional[float] = None
    swam_yesterday: Optional[bool] = None
    # 2026-09-23 (по запросу Влада): [минута_от_полуночи_ВЛ, интенсивность_движения]
    # на каждую минуту вчерашнего дня — "move bar" самого Garmin, посекундный сигнал
    # "было ли движение", раньше нигде не собирался (см. app/doctor докстрин про
    # разбор грыжи L5/S1 — «основной инструмент — ходьба каждые 30 мин»).
    movement_minutes: Optional[list[list[float]]] = None


def _js_str(v) -> str:
    """String(v) как в JS: 8000.0 -> "8000", не "8000.0" (JS не различает
    int/float — Python str() на float-е с нулевой дробной частью добавляет
    ".0", а pydantic коэрсит входящие числа в Optional[float] независимо
    от того, был ли на входе JSON-int). Без этого health.daily_trends
    получил бы "8000.0" вместо исторически привычного "8000" — тот же
    класс проблемы, что и Math.round vs Python round (_js_round1)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _rnd(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(n):
        return None
    return _js_round1(n)


def _parse_any_date_ms(s) -> Optional[float]:
    """Порт parseAnyDate — принимает DD.MM.YYYY[ HH:mm:ss], DD/MM/YYYY[...],
    ISO/'YYYY-MM-DD HH:mm:ss'; возвращает unix-ms (как JS Date.getTime())."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(.*))?$", s)
    if m:
        d, mo, y, t = m.groups()
        s2 = f"{y}-{mo.zfill(2)}-{d.zfill(2)}T{t or '00:00:00'}+10:00"
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(.*))?$", s)
        if m:
            mo, d, y, t = m.groups()
            s2 = f"{y}-{mo.zfill(2)}-{d.zfill(2)}T{t or '00:00:00'}+10:00"
        else:
            s2 = s.replace(" ", "T", 1)
            if "+" not in s2 and not s2.endswith("Z"):
                s2 += "+10:00"
    try:
        dt = datetime.fromisoformat(s2)
        return dt.timestamp() * 1000
    except ValueError:
        return None


def _to_ms(s: str) -> float:
    """bedtime/wakeup_time — тот же формат, что garmin.bedtime в оригинале
    ('YYYY-MM-DD HH:MM:SS', наивный, локальное время ВЛ)."""
    s2 = str(s).replace(" ", "T", 1)
    if "+" not in s2 and not s2.endswith("Z"):
        s2 += "+10:00"
    return datetime.fromisoformat(s2).timestamp() * 1000


def build_daily_trends_row(
    garmin: dict, nutrition_rows: list[dict], climate_rows: list[dict],
    calendar_events: list[dict], rescue_rows: list, weather: dict, weather_deltas: dict,
) -> dict:
    """Порт "Code in JavaScript" (главный узел-сборщик). `nutrition_rows` —
    health.meals за 10 дней (ключи "Date"/"Calories"/... как в оригинале),
    `climate_rows` — сырые строки MicroClimate (Дата/Температура/Влажность),
    `calendar_events` — Google Calendar API events.list items (`summary`),
    `rescue_rows` — RescueTime `rows` (список списков [time, seconds, ?, prod_level]),
    `weather` — Open-Meteo ответ целиком, `weather_deltas` — уже посчитанные
    _pressure_deltas(weather)."""
    bt_ms = wt_ms = 0.0
    if garmin.get("bedtime") and garmin.get("wakeup_time"):
        bt_ms = _to_ms(garmin["bedtime"])
        wt_ms = _to_ms(garmin["wakeup_time"])

    today_str = garmin.get("date")
    start_today_ms = datetime.fromisoformat(f"{today_str}T00:00:00+10:00").timestamp() * 1000 if today_str else 0
    start_yday_ms = start_today_ms - 24 * 3600 * 1000
    end_yday_ms = start_today_ms

    net_sleep = garmin.get("sleep_total_min") or 0
    awake = garmin.get("awake_time_min") or 0
    in_bed = net_sleep + awake
    light_sleep = net_sleep - (garmin.get("sleep_deep_min") or 0) - (garmin.get("sleep_rem_min") or 0)

    # ---- питание за вчера: ужин/завтрак/окно голода/алкоголь/кофе ----
    dinner = {"time": None, "kcal": 0.0, "p": 0.0, "f": 0.0, "c": 0.0, "fast_window": None, "total_fasting": None}
    daily_macros = {"kcal": 0.0, "p": 0.0, "f": 0.0, "c": 0.0}
    breakfast_time = None
    total_alcohol = 0.0
    last_coffee_time = None

    all_meals = []
    for r in nutrition_rows:
        raw_date = r.get("Date") or r.get("Дата") or r.get("date")
        ms = _parse_any_date_ms(raw_date)
        if ms is not None:
            all_meals.append({"row": r, "ms": ms, "raw_date": raw_date})
    all_meals.sort(key=lambda m: m["ms"])

    if all_meals and start_today_ms > 0:
        yesterdays = [m for m in all_meals if start_yday_ms <= m["ms"] < end_yday_ms]
        for item in yesterdays:
            r = item["row"]
            daily_macros["kcal"] += float(r.get("Calories") or r.get("Ккал") or r.get("Калории") or 0)
            daily_macros["p"] += float(r.get("Proteins") or r.get("Белки") or r.get("Белки, гр") or 0)
            daily_macros["f"] += float(r.get("Fats") or r.get("Жиры") or r.get("Жиры, гр") or 0)
            daily_macros["c"] += float(r.get("Carbs") or r.get("Углеводы") or r.get("Углеводы, гр") or 0)
            total_alcohol += float(r.get("Алкоголь") or r.get("Алкоголь, гр") or r.get("алкоголь, гр") or 0)
            caffeine = r.get("кофеин") or r.get("Кофеин")
            if caffeine and str(caffeine).strip() not in ("", "0"):
                last_coffee_time = item["raw_date"]

        if yesterdays:
            dinner_threshold = start_yday_ms + 17 * 3600 * 1000
            dinner_items = [m for m in yesterdays if m["ms"] >= dinner_threshold and (bt_ms == 0 or m["ms"] <= bt_ms)]
            if not dinner_items:
                before_sleep = [m for m in yesterdays if bt_ms == 0 or m["ms"] <= bt_ms]
                if before_sleep:
                    dinner_items = [before_sleep[-1]]
            if dinner_items:
                for item in dinner_items:
                    r = item["row"]
                    dinner["kcal"] += float(r.get("Calories") or r.get("Ккал") or r.get("Калории") or 0)
                    dinner["p"] += float(r.get("Proteins") or r.get("Белки") or r.get("Белки, гр") or 0)
                    dinner["f"] += float(r.get("Fats") or r.get("Жиры") or r.get("Жиры, гр") or 0)
                    dinner["c"] += float(r.get("Carbs") or r.get("Углеводы") or r.get("Углеводы, гр") or 0)
                dinner["time"] = dinner_items[-1]["raw_date"]

        if bt_ms > 0:
            before_sleep_all = [m for m in all_meals if m["ms"] <= bt_ms]
            if before_sleep_all:
                last_meal = before_sleep_all[-1]
                last_meal_ms = last_meal["ms"]
                if 0 < (bt_ms - last_meal_ms) < 24 * 3600 * 1000:
                    dinner["fast_window"] = _js_round1((bt_ms - last_meal_ms) / 3600000)
                if wt_ms > 0:
                    after_wake = [m for m in all_meals if m["ms"] >= wt_ms]
                    if after_wake:
                        first_meal = after_wake[0]
                        breakfast_time = first_meal["raw_date"]
                        if 0 < (first_meal["ms"] - last_meal_ms) < 36 * 3600 * 1000:
                            dinner["total_fasting"] = _js_round1((first_meal["ms"] - last_meal_ms) / 3600000)

    # ---- тренировки (workouts_raw: "Имя|мин|ккал;;...") ----
    t1_type = t2_type = t3_type = None
    t1_min = t2_min = t3_min = None
    workout_kcal = 0.0
    if garmin.get("workouts_raw"):
        for i, w in enumerate(str(garmin["workouts_raw"]).split(";;")):
            parts = w.split("|")
            if len(parts) == 3:
                w_name, w_min, w_kcal = parts[0], float(parts[1] or 0), float(parts[2] or 0)
                workout_kcal += w_kcal
                if i == 0:
                    t1_type, t1_min = w_name, w_min
                elif i == 1:
                    t2_type, t2_min = w_name, w_min
                elif i == 2:
                    t3_type, t3_min = w_name, w_min

    # ---- климат во время сна ----
    def _get_multi(row: dict, keys: list[str]) -> Optional[float]:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                try:
                    return float(str(v).replace(",", "."))
                except (TypeError, ValueError):
                    continue
        return None

    def _avg_multi_climate(rows: list[dict], keys: list[str]) -> Optional[float]:
        if not rows:
            return None
        total = count = 0.0
        for r in rows:
            raw_date = r.get("Дата") or r.get("Date") or r.get("date")
            if raw_date:
                ms = _parse_any_date_ms(raw_date)
                if ms is not None:
                    if bt_ms > 0 and wt_ms > 0:
                        if ms < bt_ms or ms > wt_ms:
                            continue
                    elif start_yday_ms > 0:
                        if ms < start_yday_ms or ms > end_yday_ms:
                            continue
            v = _get_multi(r, keys)
            if v is not None:
                total += v
                count += 1
        return _js_round1(total / count) if count > 0 else None

    avg_temp_c = _avg_multi_climate(climate_rows, ["temp", "Temp", "Температура", "температура", "Temperature"])
    avg_hum_pct = _avg_multi_climate(climate_rows, ["humidity", "Humidity", "Влажность", "влажность"])
    avg_pm25 = _avg_multi_climate(climate_rows, ["pm25", "PM25", "PM2.5", "pm 2.5"])
    dew_point_rows = [dict(r, **{"Точка росы": v}) for r in climate_rows
                      for v in [_dew_point(r.get("Температура"), r.get("Влажность"))] if v is not None]
    avg_dew_point = _avg_multi_climate(dew_point_rows, ["Точка_росы_avg_C", "Точка росы", "dew_point", "DewPoint"])

    # ---- лекарства из календаря ----
    meds_list = []
    for event in calendar_events:
        title = str(event.get("summary") or event.get("text") or "").lower()
        if title and (re.search(r"мг|таб|капс|\d", title)):
            if "встреча" not in title and "позвонить" not in title:
                meds_list.append((event.get("summary") or event.get("text")).strip())

    # ---- давление ночью ----
    avg_night_pressure = None
    hourly = weather.get("hourly") if weather else None
    if hourly and hourly.get("time") and hourly.get("surface_pressure") and bt_ms > 0 and wt_ms > 0:
        press_sum = press_count = 0.0
        for t_str, p in zip(hourly["time"], hourly["surface_pressure"]):
            t_ms = datetime.fromisoformat(str(t_str) + "+10:00").timestamp() * 1000
            if bt_ms <= t_ms <= wt_ms:
                press_sum += float(p)
                press_count += 1
        if press_count > 0:
            avg_night_pressure = _js_round1(press_sum / press_count)

    # ---- RescueTime (экранное время за вчера) ----
    screen_total = screen_prod = screen_distract = screen_before_bed = 0.0
    deep_work_sec = light_work_sec = 0.0
    for row in rescue_rows or []:
        r_time_str = str(row[0])
        time_spent = row[1]
        prod_level = row[3]
        r_ms = _parse_any_date_ms(r_time_str)
        if r_ms is not None and start_yday_ms <= r_ms < end_yday_ms:
            screen_total += time_spent
            if prod_level > 0:
                screen_prod += time_spent
            elif prod_level < 0:
                screen_distract += time_spent
            if prod_level == 2:
                deep_work_sec += time_spent
            elif prod_level == 1:
                light_work_sec += time_spent
        if bt_ms > 0 and r_ms is not None and r_ms <= bt_ms and (r_ms + 3600000) > (bt_ms - 2 * 3600000):
            screen_before_bed += time_spent

    cognitive_load_index = None
    if screen_total > 0:
        deep_h, light_h = deep_work_sec / 3600, light_work_sec / 3600
        score = (deep_h * 1.5) + (light_h * 0.5)
        cognitive_load_index = _js_round1(min((score / 12) * 10, 10))

    row = {
        "Дата": garmin.get("date"),
        "Время_отбоя": garmin.get("bedtime"),
        "Время_подъема": garmin.get("wakeup_time"),
        "Время_в_кровати_мин": in_bed,
        "Чистый_сон_мин": net_sleep,
        "Эффективность_сна_": f"{_js_round1((net_sleep / in_bed) * 100)}%" if in_bed > 0 else None,
        "Оценка_сна_балл": garmin.get("sleep_score"),
        "Глубокий_сон_мин": garmin.get("sleep_deep_min") or 0,
        "Глубокий_1_половина_мин": garmin.get("deep1_min") or 0,
        "Глубокий_2_половина_мин": garmin.get("deep2_min") or 0,
        "REM_сон_мин": garmin.get("sleep_rem_min") or 0,
        "Легкий_сон_мин": light_sleep if light_sleep > 0 else 0,
        "Пробуждения_кол_во": garmin.get("awake_count") or 0,
        "Беспокойные_моменты": garmin.get("restless_moments") or 0,
        "Бодрствование_мин": awake,
        "Пульс_ночной_средний": garmin.get("hr_night_avg"),
        "Пульс_ночной_мин": garmin.get("hr_night_min"),
        "Пульс_ночной_макс": garmin.get("hr_night_max"),
        "ВСР_ночная": garmin.get("hrv_night"),
        "Дыхание_ночь_среднее": garmin.get("respiration_avg"),
        "SpO2_ночь_среднее": garmin.get("spo2_avg"),
        "Восстановление_BodyBattery": garmin.get("body_battery_recharge"),
        "Стресс_дневной_средний": garmin.get("day_stress_avg"),
        "Температура_avg_C": avg_temp_c,
        "Влажность_avg_%": avg_hum_pct,
        "Точка_росы_avg_C": avg_dew_point,
        "PM25_avg": avg_pm25,
        "Питание_Всего_Ккал": _rnd(daily_macros["kcal"]),
        "Питание_Всего_Белки_г": _rnd(daily_macros["p"]),
        "Питание_Всего_Жиры_г": _rnd(daily_macros["f"]),
        "Питание_Всего_Углеводы_г": _rnd(daily_macros["c"]),
        "Время_последнего_кофе": last_coffee_time,
        "Ужин_время": dinner["time"],
        "Окно_голода_до_сна_ч": dinner["fast_window"],
        "Завтрак_время": breakfast_time,
        "Длительность_голода_ч": dinner["total_fasting"],
        "Ужин_Ккал": _rnd(dinner["kcal"]),
        "Ужин_Белки_г": _rnd(dinner["p"]),
        "Ужин_Жиры_г": _rnd(dinner["f"]),
        "Ужин_Углеводы_г": _rnd(dinner["c"]),
        "Алкоголь_гр": _rnd(total_alcohol),
        "Шаги_за_вчера": garmin.get("steps"),
        "VO2_Max": garmin.get("vo2max"),
        "Тренировка_1_Тип": t1_type or "Нет",
        "Тренировка_1_Мин": t1_min,
        "Тренировка_2_Тип": t2_type,
        "Тренировка_2_Мин": t2_min,
        "Тренировка_3_Тип": t3_type,
        "Тренировка_3_Мин": t3_min,
        "Тренировка_Ккал": workout_kcal if workout_kcal > 0 else None,
        "Тренировка_поздняя": "Да" if garmin.get("late_workout_flag") else "Нет",
        "Дыхание_тип": garmin.get("breathwork_type") or "Нет",
        "Дыхание_мин": garmin.get("breathwork_min") or 0,
        "Атмосферное_давление_ночь_hPa": avg_night_pressure,
        "Атм_давление_Дельта_12ч": _rnd(weather_deltas.get("pressure_delta_12h")) if weather_deltas.get("pressure_delta_12h") is not None else None,
        "Атм_давление_Дельта_24ч": _rnd(weather_deltas.get("pressure_delta_24h")) if weather_deltas.get("pressure_delta_24h") is not None else None,
        "Освещенность_ч_сутки": weather_deltas.get("sunshine_hours_last_24h"),
        "Лекарства_принимаемые": "\n".join(meds_list) if meds_list else None,
        "Экранное_время_всего_ч": _js_round1(screen_total / 3600) if screen_total > 0 else None,
        "Экран_Продуктивно_ч": _js_round1(screen_prod / 3600) if screen_prod > 0 else None,
        "Экран_Отвлечения_ч": _js_round1(screen_distract / 3600) if screen_distract > 0 else None,
        "Экран_перед_сном_мин": _js_round1(screen_before_bed / 60),
        "Индекс_когнитивной_нагрузки": cognitive_load_index,
        "Темп_тренировки_avg_C": garmin.get("temp_activity_avg_c"),
        "Темп_тренировки_min_C": garmin.get("temp_activity_min_c"),
        "Темп_тренировки_max_C": garmin.get("temp_activity_max_c"),
        "Training_Status": garmin.get("training_status"),
        "Training_Acute_Load": garmin.get("training_acute_load"),
        "Training_Chronic_Load": garmin.get("training_chronic_load"),
        "ACWR_Garmin": garmin.get("acwr_garmin"),
        "ACWR_Status": garmin.get("acwr_status"),
        "Garmin_устройство": garmin.get("garmin_device"),
        "Провал_без_движения_мин": longest_sedentary_gap_minutes(garmin.get("movement_minutes")),
        "Плавание_было": "Да" if garmin.get("swam_yesterday") else "Нет",
    }

    if not row["Дата"]:
        raise ValueError("Garmin ingest: пустая Дата — строку не пишем")
    # A4/анти-затирание (тот же комментарий, что в оригинале): Гармин
    # финализирует дневные итоги (шаги, VO2, training load) с лагом — ранний
    # ночной прогон отдаёт null, appendOrUpdate/UPSERT С пустым значением
    # затёр бы более полную запись прошлого прогона. Пустые поля НЕ пишем.
    return {k: v for k, v in row.items() if k == "Дата" or (v is not None and v != "")}


def _dew_point(temp_c, humidity_pct) -> Optional[float]:
    """Порт "Точка росы" (формула Магнуса). Оригинал требовал typeof number —
    в n8n Google Sheets-нода сама приводит типы ячеек; здесь sheets_client
    отдаёт сырые строки (raw values.get), поэтому коэрсим явно, тот же эффект.
    НАЙДЕНО при живой проверке (2026-09-20): MicroClimate реально хранит
    температуру с запятой ("26,4", формат сенсора/локали) — без replace
    float() падал бы на каждой строке молча (try/except глотал ValueError),
    климат-поля были бы пустыми ВСЕГДА. Тот же класс проблемы, что
    pg_sheets_diff_check.js уже чинил для чисел из Sheets."""
    try:
        t = float(str(temp_c).replace(",", "."))
        h = float(str(humidity_pct).replace(",", "."))
    except (TypeError, ValueError):
        return None
    a, b = 17.27, 237.7
    alpha = ((a * t) / (b + t)) + math.log(h / 100.0)
    return round((b * alpha) / (a - alpha), 2)


def build_upsert_sql(row: dict) -> tuple[str, list]:
    """Порт "Build DT PG" — динамический UPSERT только по присутствующим
    ключам. psycopg-параметризация вместо n8n queryReplacement устраняет
    класс бага, из-за которого падала "Write DT PG" (см. докстринг модуля)."""
    keys = [k for k in KNOWN_COLS if k in row]
    if "Дата" not in keys:
        keys.insert(0, "Дата")
    qi = lambda s: '"' + s.replace('"', '""') + '"'
    cols_sql = ", ".join(qi(k) for k in keys)
    placeholders = ", ".join("%s::date" if k == "Дата" else "%s" for k in keys)
    set_sql = ", ".join(f"{qi(k)}=EXCLUDED.{qi(k)}" for k in keys if k != "Дата")
    set_sql = (set_sql + ", " if set_sql else "") + "_synced_at=now()"
    # 2026-09-21 (инцидент, найдено при восстановлении данных за 21.09): один
    # из KNOWN_COLS — "Влажность_avg_%" — содержит буквальный %. psycopg в
    # client-side %s-биндинге сканирует ВЕСЬ текст запроса на предмет %s/%b/%t,
    # не глядя, внутри ли он кавычек — литеральный % в имени колонки (даже
    # корректно экранированной через "") ломает разбор ("got '%\"'"). Экранируем
    # % -> %% ТОЛЬКО в собранных из идентификаторов кусках (cols_sql/set_sql) —
    # сам placeholders собран из фиксированных %s-токенов, трогать не нужно.
    cols_sql = cols_sql.replace("%", "%%")
    set_sql = set_sql.replace("%", "%%")
    query = f'INSERT INTO health.daily_trends ({cols_sql}) VALUES ({placeholders}) ON CONFLICT ("Дата") DO UPDATE SET {set_sql}'
    # Все колонки, кроме "Дата", в health.daily_trends — TEXT (наследие Sheets;
    # см. \d health.daily_trends), и оригинальный JS ("Build DT PG") тоже
    # приводил КАЖДОЕ значение через String(r[k]) перед отправкой — стрингуем
    # так же, а не полагаемся на неявное приведение типов психопг.
    params = [str(row[k])[:10] if k == "Дата" else _js_str(row[k]) for k in keys]
    return query, params


# =====================================================================
# Внешние источники (RescueTime/Open-Meteo/Calendar/Sheets/Postgres)
# =====================================================================

def fetch_nutrition(cur) -> list[dict]:
    cur.execute(
        "SELECT m.*, to_char(m.\"Date\" AT TIME ZONE %s, 'YYYY-MM-DD\"T\"HH24:MI') AS \"Date\" "
        'FROM health.meals m ORDER BY m."Date"',
        (timeutil.person_tz_name(),),
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_climate() -> list[dict]:
    from app.sheets_client import get_values
    rows = get_values(MICROCLIMATE_SHEET_ID, MICROCLIMATE_RANGE)
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:]]


def fetch_calendar_events(day_iso: str) -> list[dict]:
    from app.sheets_client import get_calendar_events
    time_min = f"{day_iso}T00:00:00Z"
    time_max = f"{day_iso}T23:59:59Z"
    return get_calendar_events(CALENDAR_ID, time_min, time_max)


def fetch_rescuetime(day_iso: str) -> list:
    api_key = os.environ.get("RESCUETIME_API_KEY")
    if not api_key:
        logger.warning("biohacking_ingest: RESCUETIME_API_KEY не задан, пропускаю")
        return []
    yday = (date.fromisoformat(day_iso) - timedelta(days=1)).isoformat()
    try:
        resp = httpx.get(
            "https://www.rescuetime.com/anapi/data",
            params={"key": api_key, "format": "json", "rs": "hour", "rk": "productivity",
                    "restrict_begin": yday, "restrict_end": day_iso, "pv": "interval"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json().get("rows", [])
    except Exception:
        logger.exception("biohacking_ingest: RescueTime недоступен")
        return []


def fetch_weather() -> dict:
    resp = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": WEATHER_LAT, "longitude": WEATHER_LON,
                "hourly": "surface_pressure,sunshine_duration,shortwave_radiation",
                "past_days": 1, "forecast_days": 1, "timezone": timeutil.home_tz_name()},
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


def compute_pressure_deltas(weather: dict) -> dict:
    """Порт "Перепады давления".

    L6 (аудит логики, 2026-09-23, найдено по пути): fetch_weather() запрашивает
    Open-Meteo с `timezone=home_tz_name()` — hourly.time приходит НАИВНЫМИ
    строками в ДОМАШНЕМ местном времени (Владивосток), без суффикса зоны (тот
    же формат, что и avg_night_pressure ниже по файлу уже парсит правильно,
    через "+10:00"). Эта функция трактовала те же строки как UTC (+00:00) —
    якорь "текущего часа" уезжал примерно на 10 часов, idx почти никогда не
    совпадал с реальным "сейчас", код тихо падал в fallback "середина массива"
    (idx = len(times)//2) — Атм_давление_Дельта_12ч/24ч считались от
    произвольного часа, не от реального "сейчас"."""
    hourly = weather.get("hourly") or {}
    pressure, times = hourly.get("surface_pressure") or [], hourly.get("time") or []
    if not pressure or not times:
        return {}
    home_tz = ZoneInfo(timeutil.home_tz_name())
    now = datetime.now(timezone.utc).astimezone(home_tz)
    idx = next((i for i, t in enumerate(times)
                if datetime.fromisoformat(str(t)).replace(tzinfo=home_tz).hour == now.hour
                and datetime.fromisoformat(str(t)).date() == now.date()), -1)
    if idx == -1:
        idx = len(times) // 2
    if idx < 24:
        idx = 24
    if idx >= len(pressure) or idx - 24 < 0:
        return {}
    current, p24, p12 = pressure[idx], pressure[idx - 24], pressure[idx - 12]
    sunshine = hourly.get("sunshine_duration") or []
    sunshine_total = sum(sunshine[max(0, idx - 24):idx]) if sunshine else 0
    return {
        "current_pressure": current,
        "pressure_delta_24h": current - p24,
        "pressure_delta_12h": current - p12,
        "sunshine_hours_last_24h": round((sunshine_total / 3600) * 10) / 10,
    }


def write_daily_trends(cur, row: dict) -> None:
    query, params = build_upsert_sql(row)
    cur.execute(query, params)


# sync_to_sheets() — УДАЛЕНА 2026-09-23 (постепенный отказ от Sheets,
# категория A, по запросу Влада: "с нокодб я могу смотреть данные прямо в
# постгре"). health.daily_trends и так был единственным каноном (Sheets —
# чистое зеркало для глаз, appendOrUpdate по "Дата") — NocoDB закрывает
# ровно ту же потребность прямо на Postgres.

# METRIC_COLS — тот же справочник, что в оригинальном "Build DT PG" (device-факты
# baseline/reference + расширение 14.09.2026), перенесён без изменений порядка/состава.
METRIC_COLS = {
    "hrv": "ВСР_ночная", "rhr": "Пульс_ночной_средний", "body_battery": "Восстановление_BodyBattery",
    "sleep_min": "Чистый_сон_мин", "sleep_score": "Оценка_сна_балл", "sleep_eff": "Эффективность_сна_",
    "stress": "Стресс_дневной_средний", "steps": "Шаги_за_вчера", "vo2max": "VO2_Max",
    "respiration": "Дыхание_ночь_среднее", "spo2": "SpO2_ночь_среднее",
    "time_in_bed_min": "Время_в_кровати_мин", "deep_sleep_min": "Глубокий_сон_мин",
    "deep_sleep_1st_half_min": "Глубокий_1_половина_мин", "deep_sleep_2nd_half_min": "Глубокий_2_половина_мин",
    "rem_sleep_min": "REM_сон_мин", "light_sleep_min": "Легкий_сон_мин",
    "awakenings_count": "Пробуждения_кол_во", "wake_min": "Бодрствование_мин",
    "rhr_min": "Пульс_ночной_мин", "rhr_max": "Пульс_ночной_макс", "restless_moments": "Беспокойные_моменты",
    "temp_avg_c": "Температура_avg_C", "humidity_avg_pct": "Влажность_avg_%", "pm25_avg": "PM25_avg",
    "fasting_window_before_sleep_h": "Окно_голода_до_сна_ч", "fasting_duration_h": "Длительность_голода_ч",
    "dinner_kcal": "Ужин_Ккал", "dinner_protein_g": "Ужин_Белки_г", "dinner_fat_g": "Ужин_Жиры_г",
    "dinner_carbs_g": "Ужин_Углеводы_г", "workout_kcal": "Тренировка_Ккал",
    "pressure_night_hpa": "Атмосферное_давление_ночь_hPa", "alcohol_g": "Алкоголь_гр",
    "screen_total_h": "Экранное_время_всего_ч", "screen_productive_h": "Экран_Продуктивно_ч",
    "screen_distraction_h": "Экран_Отвлечения_ч", "respiration_min": "Дыхание_мин",
    "screen_before_sleep_min": "Экран_перед_сном_мин",
    "nutrition_total_kcal": "Питание_Всего_Ккал", "nutrition_total_protein_g": "Питание_Всего_Белки_г",
    "nutrition_total_fat_g": "Питание_Всего_Жиры_г", "nutrition_total_carbs_g": "Питание_Всего_Углеводы_г",
    "workout1_min": "Тренировка_1_Мин", "workout2_min": "Тренировка_2_Мин", "workout3_min": "Тренировка_3_Мин",
    "workout_temp_avg_c": "Темп_тренировки_avg_C", "workout_temp_min_c": "Темп_тренировки_min_C",
    "workout_temp_max_c": "Темп_тренировки_max_C",
    "training_acute_load": "Training_Acute_Load", "training_chronic_load": "Training_Chronic_Load",
    "dew_point_avg_c": "Точка_росы_avg_C", "pressure_delta_12h": "Атм_давление_Дельта_12ч",
    "pressure_delta_24h": "Атм_давление_Дельта_24ч", "daylight_h": "Освещенность_ч_сутки",
    "cognitive_load_index": "Индекс_когнитивной_нагрузки", "acwr": "ACWR_Garmin",
}


def sync_device_facts(row: dict) -> None:
    """Было fire-and-forget HTTP POST card-service:8080/facts/device на
    самого себя (n8n и card-service были разными процессами) — теперь то
    же самое одним прямым вызовом в процессе, без HTTP-круга."""
    from app.main import StructuredFact, _write_structured_facts

    day_iso = str(row.get("Дата"))[:10]
    facts = []
    for key, col in METRIC_COLS.items():
        raw = row.get(col)
        if raw is None or raw == "":
            continue
        try:
            num = float(str(raw).replace(",", "."))
        except ValueError:
            continue
        facts.append(StructuredFact(metric_key=key, value_num=num, ts_event=f"{day_iso}T00:00:00Z"))
    if facts:
        try:
            _write_structured_facts(facts, origin="device")
        except Exception:
            logger.exception("biohacking_ingest: не удалось синхронизировать device-факты (не блокирует запись daily_trends)")


# Премортем (2026-09-20, задача "1,3,4,5,7", проблема #7 "хроническая
# нестабильность Гарминовского пайплайна лечилась точечно, не системно"):
# health.garmin_ingest_log хранит СЫРОЙ payload ПЕРВЫМ делом, до всякой
# обработки. Раньше эта граница ломалась несколько раз по-разному (лаг
# Гармина + null-clobber, задвоение вебхука, multiline-баг n8n-ноды записи,
# запятая-десятичная в MicroClimate) — каждый раз чинили конкретный симптом.
# Теперь сбой на ЛЮБОМ шаге сборки/записи не теряет сырые данные — их можно
# безопасно переиграть через reprocess_from_log(), без запуска send_to_n8n.py
# вручную (CLAUDE.md): UPSERT в daily_trends идемпотентен по "Дата", повторная
# обработка того же дня не создаёт дублей, только обновляет существующую
# строку — сама причина запрета "не запускай вручную" (риск задвоения) здесь
# структурно не может случиться.
def _log_raw_ingest(cur, garmin: dict) -> int:
    cur.execute(
        "INSERT INTO health.garmin_ingest_log (date, raw_payload) VALUES (%s, %s::jsonb) RETURNING id",
        (garmin.get("date"), json.dumps(garmin, ensure_ascii=False, default=str)),
    )
    return cur.fetchone()[0]


def _mark_ingest_result(cur, log_id: int, status: str, error: Optional[str] = None) -> None:
    cur.execute(
        "UPDATE health.garmin_ingest_log SET status=%s, error=%s, processed_at=now() WHERE id=%s",
        (status, error, log_id),
    )


def _build_and_write(garmin: dict) -> dict:
    """Собственно вся обработка (было единственным телом process_ingest до
    добавления raw-лога) — вынесена отдельно, чтобы process_ingest() и
    reprocess_from_log() звали одно и то же, не дублируя цепочку."""
    day_iso = garmin["date"]

    with get_conn() as conn, conn.cursor() as cur:
        nutrition_rows = fetch_nutrition(cur)

    climate_rows = []
    try:
        climate_rows = fetch_climate()
    except Exception:
        logger.exception("biohacking_ingest: MicroClimate недоступен, климат-поля будут пустыми")

    calendar_events = []
    try:
        calendar_events = fetch_calendar_events(day_iso)
    except Exception:
        logger.exception("biohacking_ingest: Google Calendar недоступен, Лекарства_принимаемые будет пустым")

    rescue_rows = fetch_rescuetime(day_iso)

    weather, weather_deltas = {}, {}
    try:
        weather = fetch_weather()
        weather_deltas = compute_pressure_deltas(weather)
    except Exception:
        logger.exception("biohacking_ingest: Open-Meteo недоступен, давление/освещённость будут пустыми")

    row = build_daily_trends_row(garmin, nutrition_rows, climate_rows, calendar_events, rescue_rows, weather, weather_deltas)

    with get_conn() as conn, conn.cursor() as cur:
        write_daily_trends(cur, row)
        conn.commit()

    sync_device_facts(row)

    from app import anomaly_detector
    try:
        anomaly_detector.run_daily_check()
    except Exception:
        logger.exception("biohacking_ingest: run_daily_check упал — daily_trends уже записан, не блокируем ответ")

    return row


def process_ingest(payload: BiohackingPayload) -> dict:
    """Оркестратор — порт всей цепочки Webhook -> ... -> Write DT PG ->
    Call 'Anomaly_Detector'. Возвращает записанную строку (для теста/ответа
    эндпоинта). Сырой payload логируется ДО обработки (см. комментарий выше
    про garmin_ingest_log) — если что-то ниже упадёт, лог остаётся с
    status='failed' и его можно переиграть через reprocess_from_log(), не
    запуская send_to_n8n.py заново."""
    garmin = payload.model_dump(exclude_none=False)

    with get_conn() as conn, conn.cursor() as cur:
        log_id = _log_raw_ingest(cur, garmin)
        conn.commit()

    try:
        row = _build_and_write(garmin)
    except Exception as e:
        with get_conn() as conn, conn.cursor() as cur:
            _mark_ingest_result(cur, log_id, "failed", str(e)[:2000])
            conn.commit()
        raise

    with get_conn() as conn, conn.cursor() as cur:
        _mark_ingest_result(cur, log_id, "done")
        conn.commit()
    return row


def reprocess_from_log(log_id: int) -> dict:
    """Ручная переигровка сырого payload'а из health.garmin_ingest_log —
    безопасная альтернатива повторному запуску send_to_n8n.py: UPSERT в
    health.daily_trends идемпотентен по "Дата", повторная обработка того же
    дня не плодит дубли, только обновляет существующую строку. Не вызывается
    автоматически ни из чего — сознательно ручной инструмент на случай
    реального сбоя (используй из python-консоли в контейнере, как и другие
    разовые операции в этом проекте)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT raw_payload FROM health.garmin_ingest_log WHERE id = %s", (log_id,))
        found = cur.fetchone()
    if not found:
        raise ValueError(f"garmin_ingest_log id={log_id} не найден")
    garmin = found[0]
    row = _build_and_write(garmin)
    with get_conn() as conn, conn.cursor() as cur:
        _mark_ingest_result(cur, log_id, "done (replay)")
        conn.commit()
    return row
