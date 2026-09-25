"""
Реестр инструментов агентного цикла (план §3.4, §3.9): схема (OpenAI tool-calling
формат, план §3.4 — "все кандидаты возвращают структурные вызовы") + исполнитель
+ таймаут + read_only. Каждый исполнитель — `(cur, args: dict) -> dict`, даже
если конкретному инструменту курсор/аргументы не нужны — единый интерфейс для
loop.py (Phase 4), которому нужно вызывать любой инструмент одинаково.

Портированы из старого доктора (план §2.3, §1 п.12), минус Google Calendar
(Влад: «через доктора не использую календарь») и минус n8n-специфика:
- Read_Symptoms/Read_Investigations/Get_Patient_Medical_History/Get_Meals_Today:
  были Sheets/HTTP-инструментами агента — теперь прямой SELECT.
- Get_Outdoor_Weather: был toolHttpRequest на open-meteo — тот же URL, прямой httpx.
- Get_Room_Climate_Now: был единственным оставленным мостом в n8n (план §2.3) —
  закрыт 2026-09-20 (#28), теперь прямой SELECT из health.microclimate.
- Nutrition_Analyzer/Analyze_Symptom_Food: были executeWorkflowTrigger на
  суб-воркфлоу — портированы как функции, алгоритм не менялся.

Новое сверх старого набора (план §7, "Чего у него нет, хотя данные есть"):
Read_Labs (257 строк card.lab_result, раньше видны только как "вне референса"
текстом) и Read_Garmin_History (раньше — только "вчера", в статичном досье).

Write-инструменты (план §3.5, ключевое отличие от старого доктора — записи это
инструменты, а не поля JSON-конверта): их исполнители в Phase 4 НЕ пишут в БД —
только валидируют аргументы через pydantic (contract.py) и возвращают
StagedWrite-совместимый payload. Настоящая запись (транзакция + инварианты —
одно открытое расследование и т.п.) — commit.py, Phase 5. До тех пор loop.py
собирает staged_writes и ничего не коммитит — безопасно, т.к. новый доктор ещё
не подключён к живому трафику (план §3.9, cutover — Phase 7)."""
from datetime import datetime
from typing import Optional

import httpx
from psycopg import sql
from pydantic import ValidationError

from app.doctor.contract import (
    CloseInvestigationArgs, CloseRecommendationArgs, OpenInvestigationArgs, PlanLabArgs,
    RecordNoteArgs, RecordSymptomArgs, UpdateInvestigationArgs,
)

from app import timeutil
from app.db import schema
from app.doctor.context import _num, _room_climate

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=43.1198&longitude=131.8869"
    "&current=temperature_2m,relative_humidity_2m,precipitation,weather_code"
    "&timezone=Asia%2FVladivostok"
)

NUTRITION_NUMERIC_FIELDS = [
    "Calories", "Proteins", "Carbs", "Fats", "Магний", "Витамин D",
    "Омега-3 (EPA/DHA)", "Селен", "Йод", "Калий", "Железо", "Кальций",
    "Витамин B12", "Витамин К", "Витамин Е", "Цинк",
]

SYMPTOM_FOOD_NUTR = [
    "Calories", "Proteins", "Carbs", "Fats", "Насыщенные жиры", "Трансжиры",
    "Добавленный сахар", "Кофеин", "Алкоголь", "Клетчатка", "Холестерин", "Натрий",
]
SYMPTOM_FOOD_WINDOW_H = 6
SYMPTOM_FOOD_STOP = {
    "около", "грамм", "граммов", "штук", "штука", "ложка", "ложки", "ложек",
    "стакан", "порция", "порции", "вес", "итого", "примерно", "свежий",
    "свежая", "свежие", "сырой", "сырая", "домашний", "домашняя", "кусок",
    "кусочек", "большой", "маленький", "средний",
}


# --- read-only инструменты, прямой SELECT -----------------------------------

def read_symptoms(cur, args: dict) -> dict:
    cur.execute(
        "SELECT symptom_id, to_char(ts, 'YYYY-MM-DD\"T\"HH24:MI') AS ts, symptom, "
        "system, severity, status, change, domain "
        "FROM health.symptom_log ORDER BY ts DESC LIMIT 50"
    )
    cols = ["symptom_id", "ts", "symptom", "system", "severity", "status", "change", "domain"]
    return {"symptoms": [dict(zip(cols, row)) for row in cur.fetchall()]}


def read_investigations(cur, args: dict) -> dict:
    cur.execute(
        "SELECT inv_id, trigger, trigger_detail, hypothesis, status, findings, "
        "doctor_brief, to_char(opened,'YYYY-MM-DD') AS opened, "
        "to_char(updated,'YYYY-MM-DD') AS updated FROM health.investigations "
        "ORDER BY opened DESC LIMIT 20"
    )
    cols = ["inv_id", "trigger", "trigger_detail", "hypothesis", "status", "findings",
            "doctor_brief", "opened", "updated"]
    return {"investigations": [dict(zip(cols, row)) for row in cur.fetchall()]}


def get_patient_medical_history(cur, args: dict) -> dict:
    limit = min(int(args.get("limit", 20) or 20), 50)
    cur.execute(
        "SELECT to_char(note_date,'YYYY-MM-DD') AS d, category, note, trigger, plan "
        "FROM health.doctor_notes ORDER BY note_date DESC LIMIT %s",
        (limit,),
    )
    cols = ["date", "category", "note", "trigger", "plan"]
    return {"notes": [dict(zip(cols, row)) for row in cur.fetchall()]}


def get_meals_today(cur, args: dict) -> dict:
    tz = timeutil.person_tz_name()
    cur.execute(
        "SELECT to_char(\"Date\" AT TIME ZONE %s, 'HH24:MI') AS t, "
        '"Meal_description", "Calories", "Proteins", "Fats", "Carbs" '
        'FROM health.meals '
        "WHERE (\"Date\" AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date "
        'ORDER BY "Date"',
        (tz, tz, tz),
    )
    meals = [
        {"time": t, "description": d, "kcal": _num(k), "protein_g": _num(p),
         "fats_g": _num(f), "carbs_g": _num(c)}
        for t, d, k, p, f, c in cur.fetchall()
    ]
    return {"meals": meals, "count": len(meals)}


def read_labs(cur, args: dict) -> dict:
    """Новое (план §7): раньше доктор видел лабы только как "вне референса"
    текстом в горячем слое — здесь полный список последних результатов по
    каждому маркеру, с опциональным фильтром по названию."""
    marker_filter = (args.get("marker") or "").strip().lower()
    limit = min(int(args.get("limit", 30) or 30), 100)
    q = sql.SQL(
        "SELECT DISTINCT ON (marker_key) marker_key, marker_label, value_num, value_text, "
        "unit, ref_min, ref_max, ts_event FROM {t} "
        "WHERE (%s = '' OR lower(coalesce(marker_label, marker_key)) LIKE '%%' || %s || '%%') "
        "ORDER BY marker_key, ts_event DESC"
    ).format(t=sql.Identifier(schema(), "lab_result"))
    cur.execute(q, (marker_filter, marker_filter))
    rows = cur.fetchall()[:limit]
    out = []
    for key, label, value, value_text, unit, lo, hi, ts in rows:
        out.append({
            "marker": label or key, "value": float(value) if value is not None else value_text,
            "unit": unit, "ref_min": float(lo) if lo is not None else None,
            "ref_max": float(hi) if hi is not None else None, "date": str(ts)[:10],
        })
    return {"labs": out}


def read_garmin_history(cur, args: dict) -> dict:
    """Новое (план §7): раньше — только "вчера" в статичном досье. Здесь —
    произвольная глубина (по умолчанию 30 дней), для вопросов вида "как менялся
    сон за последний месяц"."""
    days = min(int(args.get("days", 30) or 30), 180)
    cur.execute(
        'SELECT "Дата", "Чистый_сон_мин", "Пульс_ночной_средний", "Оценка_сна_балл", '
        '"Шаги_за_вчера", "Тренировка_Ккал" '
        'FROM health.daily_trends ORDER BY "Дата" DESC LIMIT %s',
        (days,),
    )
    rows = cur.fetchall()
    history = [
        {"date": str(d), "sleep_min": _num(s), "resting_hr": _num(hr),
         "sleep_score": _num(sc), "steps": _num(st), "training_kcal": _num(tk)}
        for d, s, hr, sc, st, tk in rows
    ]
    history.reverse()  # от старых к новым — так удобнее описывать тренд
    return {"days": len(history), "history": history}


def get_outdoor_weather(cur, args: dict) -> dict:
    resp = httpx.get(OPEN_METEO_URL, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


def get_room_climate_now(cur, args: dict) -> dict:
    """2026-09-20 (#28): читает health.microclimate напрямую — n8n-мост
    (room-climate-now) убран, см. app.doctor.context._room_climate."""
    value = _room_climate(cur)
    return value if value is not None else {"error": "no_data"}


# --- Nutrition Analyzer (портирован из Sub-Agent: Nutrition Analyzer) -------

def _nutrition_date_with_shift(dt: Optional[datetime], tz) -> Optional[datetime]:
    """Приёмы после полуночи до 2:00 считаются предыдущим днём («поздний ужин») —
    та же логика, что была в n8n-версии, но по ЛОКАЛЬНОМУ часу человека.

    T2 (внешний аудит логики, 2026-09-22): раньше условие `dt.hour < 2`
    сравнивалось по UTC — во Владивостоке местные 00:00–02:00 это 14:00–16:00
    UTC, правило не срабатывало НИКОГДА; зато ошибочно сдвигало на вчера
    местную еду 10:00–12:00 (UTC 00–01). Заодно чинится и дата без сдвига:
    возвращаем день в зоне человека (раньше `.date()` был UTC-днём — для еды
    до 10:00 VL это тоже был вчерашний день). Возврат — наивный datetime в
    полдень нужного дня (вызывающий использует только .date())."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.astimezone(tz).date()
    if dt.astimezone(tz).hour < 2:
        from datetime import timedelta
        day = day - timedelta(days=1)
    return datetime(day.year, day.month, day.day, 12, 0, 0)


def analyze_nutrition_stability(cur, args: dict) -> dict:
    """Портировано 1:1 из Sub-Agent: Nutrition Analyzer (Code in JavaScript):
    среднее в день по нутриентам + стабильность калорий (100 - коэфф. вариации)
    за последние 7 полных дней ДО самой свежей даты в health.meals."""
    tz = timeutil.person_tz()
    cur.execute(
        'SELECT "User_ID", "Date", "Calories", "Proteins", "Carbs", "Fats", "Магний", '
        '"Витамин D", "Омега-3 (EPA/DHA)", "Селен", "Йод", "Калий", "Железо", "Кальций", '
        '"Витамин B12", "Витамин К", "Витамин Е", "Цинк" FROM health.meals'
    )
    rows = cur.fetchall()
    if not rows:
        return {"error": "Нет данных для анализа"}

    cols = ["User_ID", "Date"] + NUTRITION_NUMERIC_FIELDS
    parsed = [dict(zip(cols, r)) for r in rows]

    shifted = [(_nutrition_date_with_shift(r["Date"], tz), r) for r in parsed]
    shifted = [(d, r) for d, r in shifted if d is not None]
    if not shifted:
        return {"error": "Не удалось определить даты в данных"}
    max_date = max(d for d, _ in shifted).date()

    by_user_day: dict[tuple, dict] = {}
    for d, r in shifted:
        day = d.date()
        if day == max_date:
            continue
        if (max_date - day).days > 7:
            continue
        user = r["User_ID"] or "self"
        key = (user, day)
        bucket = by_user_day.setdefault(key, {f: 0.0 for f in NUTRITION_NUMERIC_FIELDS})
        for f in NUTRITION_NUMERIC_FIELDS:
            bucket[f] += _num(r[f]) or 0.0

    by_user: dict[str, list] = {}
    for (user, day), sums in by_user_day.items():
        by_user.setdefault(user, []).append(sums)

    results = []
    for user, days in by_user.items():
        days_count = len(days)
        averages = {f: round(sum(d[f] for d in days) / days_count, 1) for f in NUTRITION_NUMERIC_FIELDS}
        cal_values = [d["Calories"] for d in days]
        if len(cal_values) >= 2 and (mean := sum(cal_values) / len(cal_values)) > 0:
            variance = sum((v - mean) ** 2 for v in cal_values) / len(cal_values)
            cv_pct = (variance ** 0.5 / mean) * 100
            stability = round(max(0.0, 100 - cv_pct), 1)
        else:
            stability = None
        results.append({
            "user": user, "days_with_data": days_count,
            "calorie_stability_pct": stability, **averages,
        })
    return {"date": str(max_date), "users": results}


# --- Analyze_Symptom_Food (портирован из Analyze_Symptom_Food) -------------

def _symptom_food_ts(v) -> Optional[float]:
    if v is None:
        return None
    return v.timestamp() if hasattr(v, "timestamp") else None


def _symptom_food_words(s: str) -> set[str]:
    import re
    words = set(re.findall(r"[а-яёa-z]{4,}", (s or "").lower()))
    trimmed = set()
    for w in words:
        w2 = re.sub(r"(ый|ая|ое|ые|ой|ов|ам|ах|у|е|а|ы|и|я)$", "", w)
        if len(w2) >= 4 and w2 not in SYMPTOM_FOOD_STOP:
            trimmed.add(w2)
    return trimmed


def analyze_symptom_food(cur, args: dict) -> dict:
    """Портировано 1:1 из Analyze_Symptom_Food (Analyze code node) — эпизоды
    симптома vs приёмы пищи в pre-окне (6ч до эпизода) vs база (тот же час суток,
    вне pre-окна). НЕ доказательство, генератор кандидатов для элиминационного
    теста — та же оговорка, что и в оригинале."""
    symptom_id = (args.get("symptom_id") or "").strip()
    if not symptom_id:
        return {"error": "no_symptom_id"}

    cur.execute(
        "SELECT ts, severity, notes, symptom FROM health.symptom_log "
        "WHERE symptom_id = %s ORDER BY ts",
        (symptom_id,),
    )
    episodes = [
        {"at": ts.timestamp(), "severity": float(sev) if sev is not None else None,
         "note": notes or symptom}
        for ts, sev, notes, symptom in cur.fetchall()
    ]
    if not episodes:
        return {"symptom_id": symptom_id, "episodes": 0,
                "note": "Нет записанных эпизодов этого симптома в symptom_log."}

    cur.execute(
        'SELECT "Date", "Meal_description", "Calories", "Proteins", "Carbs", "Fats", '
        '"Насыщенные жиры"::text, "Трансжиры"::text, "Добавленный сахар"::text, "Кофеин"::text, '
        '"Алкоголь"::text, "Клетчатка"::text, "Холестерин"::text, "Натрий"::text FROM health.meals'
    )
    meal_cols = ["Calories", "Proteins", "Carbs", "Fats", "Насыщенные жиры", "Трансжиры",
                 "Добавленный сахар", "Кофеин", "Алкоголь", "Клетчатка", "Холестерин", "Натрий"]
    meals = []
    for row in cur.fetchall():
        dt, desc, *nutr_vals = row
        if dt is None:
            continue
        meals.append({"at": dt.timestamp(), "desc": desc or "",
                      "n": dict(zip(meal_cols, [_num(v) for v in nutr_vals]))})
    meals.sort(key=lambda m: m["at"])

    window_s = SYMPTOM_FOOD_WINDOW_H * 3600
    pre_idx = set()
    hits_per_episode = []
    for ep in episodes:
        hits = [i for i, m in enumerate(meals) if ep["at"] - window_s <= m["at"] <= ep["at"]]
        pre_idx.update(hits)
        hits_per_episode.append(len(hits))

    def tod_bucket(ts: float) -> str:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        h = _dt.fromtimestamp(ts, _tz(_td(hours=10))).hour
        if 5 <= h < 11:
            return "утро"
        if 11 <= h < 16:
            return "день"
        if 16 <= h < 23:
            return "вечер"
        return "ночь"

    pre = [meals[i] for i in sorted(pre_idx)]
    pre_buckets = {tod_bucket(m["at"]) for m in pre}
    base = [m for i, m in enumerate(meals) if i not in pre_idx and tod_bucket(m["at"]) in pre_buckets]
    base_matched_by = f"то же время суток ({'/'.join(sorted(pre_buckets))})"
    if len(base) < 3:
        base = [m for i, m in enumerate(meals) if i not in pre_idx]
        base_matched_by = "все приёмы — в то же время суток данных мало, возможна смещённость по времени дня"

    if len(pre) < 2:
        return {"symptom_id": symptom_id, "episodes": len(episodes), "pre_meals": len(pre),
                "note": "Слишком мало приёмов пищи в окнах перед эпизодами. "
                        "Проси пациента отмечать время боли."}

    def mean_of(rows, key):
        vals = [r["n"][key] for r in rows if r["n"].get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    nutrient_candidates = []
    for key in SYMPTOM_FOOD_NUTR:
        p, b = mean_of(pre, key), mean_of(base, key)
        if p is None or b is None:
            continue
        ratio = 99.0 if b == 0 and p > 0 else (round(p / b, 2) if b != 0 else 1.0)
        if ratio == 99.0 or ratio >= 1.4 or ratio <= 0.7:
            nutrient_candidates.append({
                "factor": key, "pre_mean": round(p, 2), "base_mean": round(b, 2),
                "ratio": "∞" if ratio == 99.0 else ratio,
                "direction": "выше/есть перед эпизодом" if (b == 0 and p > 0) or ratio >= 1 else "ниже перед эпизодом",
            })

    pre_words: dict[str, int] = {}
    base_words: dict[str, int] = {}
    for m in pre:
        for w in _symptom_food_words(m["desc"]):
            pre_words[w] = pre_words.get(w, 0) + 1
    for m in base:
        for w in _symptom_food_words(m["desc"]):
            base_words[w] = base_words.get(w, 0) + 1

    food_candidates = []
    for w, count in pre_words.items():
        pf = count / len(pre)
        bf = base_words.get(w, 0) / max(len(base), 1)
        lift = round(pf / bf, 2) if bf > 0 else (99.0 if pf > 0 else 0.0)
        if count >= 2 and lift >= 1.8:
            food_candidates.append({"food": w, "in_pre_meals": count, "pre_freq": round(pf, 2),
                                    "base_freq": round(bf, 2), "lift": lift})
    food_candidates.sort(key=lambda x: -x["lift"])

    enough = len(episodes) >= 4 and len(pre) >= 4
    return {
        "symptom_id": symptom_id, "episodes": len(episodes),
        "episodes_with_meal_data": sum(1 for h in hits_per_episode if h > 0),
        "pre_meals": len(pre), "base_meals": len(base), "base_matched_by": base_matched_by,
        "window_hours": SYMPTOM_FOOD_WINDOW_H,
        "nutrient_candidates": nutrient_candidates[:8], "food_candidates": food_candidates[:10],
        "confidence": "предварительно" if enough else "очень низкая (мало эпизодов)",
        "note": "Ассоциативный анализ, НЕ доказательство причинности. Каждый кандидат "
                "проверяется элиминационным тестом. При <4 эпизодах выводы почти случайны.",
    }


# --- write-инструменты: только валидация + staging, commit.py пишет (Phase 5) -

def _stage(kind: str, model_cls, args: dict) -> dict:
    try:
        validated = model_cls(**args)
    except ValidationError as e:
        return {"error": "invalid_arguments", "detail": str(e)}
    return {"staged": True, "kind": kind, "payload": validated.model_dump()}


def record_symptom(cur, args: dict) -> dict:
    return _stage("symptom", RecordSymptomArgs, args)


def record_note(cur, args: dict) -> dict:
    return _stage("note", RecordNoteArgs, args)


def open_investigation(cur, args: dict) -> dict:
    return _stage("investigation_open", OpenInvestigationArgs, args)


def update_investigation(cur, args: dict) -> dict:
    return _stage("investigation_update", UpdateInvestigationArgs, args)


def close_investigation(cur, args: dict) -> dict:
    return _stage("investigation_close", CloseInvestigationArgs, args)


def plan_lab(cur, args: dict) -> dict:
    return _stage("lab_plan", PlanLabArgs, args)


def close_recommendation(cur, args: dict) -> dict:
    return _stage("recommendation_close", CloseRecommendationArgs, args)


# --- реестр ------------------------------------------------------------------

TOOL_REGISTRY = [
    {
        "name": "Read_Symptoms",
        "description": "История ранее записанных симптомов. Вызывай ПЕРЕД записью нового "
                        "симптома — понять, новый он или продолжение уже отслеживаемого.",
        "parameters": {"type": "object", "properties": {}},
        "executor": read_symptoms, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Read_Investigations",
        "description": "Список расследований. Вызывай ПЕРЕД тем как открыть новое — "
                        "одновременно только одно открытое.",
        "parameters": {"type": "object", "properties": {}},
        "executor": read_investigations, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Get_Patient_Medical_History",
        "description": "История прошлых обращений (заметки врача), прошлые триггеры и "
                        "реакции на лечение. Используй при обострении/новых симптомах — "
                        "сопоставь текущую жалобу с историей.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Сколько последних записей вернуть (по умолчанию 20)"},
        }},
        "executor": get_patient_medical_history, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Get_Meals_Today",
        "description": "Приёмы пищи пациента СЕГОДНЯ с точным временем и составом "
                        "(белки/жиры/углеводы/ккал по каждому приёму). Вызывай, если жалоба "
                        "может быть связана с едой — не спрашивай то, что можно узнать самому.",
        "parameters": {"type": "object", "properties": {}},
        "executor": get_meals_today, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Get_Room_Climate_Now",
        "description": "Последнее показание датчика климата в спальне (температура, "
                        "влажность, PM2.5), обновляется раз в час. Вызывай перед тем как "
                        "советовать что-то про влажность/воздух в помещении.",
        "parameters": {"type": "object", "properties": {}},
        "executor": get_room_climate_now, "timeout": 5.0, "read_only": True,
    },
    {
        "name": "Get_Outdoor_Weather",
        "description": "Текущая погода во Владивостоке (температура, влажность, осадки). "
                        "Вызывай, если совет про влажность/воздух стоит сверить с уличными условиями.",
        "parameters": {"type": "object", "properties": {}},
        "executor": get_outdoor_weather, "timeout": 5.0, "read_only": True,
    },
    {
        "name": "Nutrition_Analyzer",
        "description": "Среднесуточное потребление нутриентов и стабильность калоража за "
                        "последние 7 дней. Используй при жалобах, которые могут быть связаны "
                        "с хроническим дефицитом/избытком, а не с конкретным приёмом пищи.",
        "parameters": {"type": "object", "properties": {}},
        "executor": analyze_nutrition_stability, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Analyze_Symptom_Food",
        "description": "Ассоциативный анализ «этот симптом ↔ еда перед эпизодами» по "
                        "symptom_id — сравнивает приёмы пищи в 6ч-окне перед эпизодами с "
                        "обычными приёмами в то же время суток. Не доказательство, генератор "
                        "кандидатов для элиминационного теста.",
        "parameters": {"type": "object", "properties": {
            "symptom_id": {"type": "string", "description": "ID симптома из Read_Symptoms"},
        }, "required": ["symptom_id"]},
        "executor": analyze_symptom_food, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Read_Labs",
        "description": "Последние результаты лабораторных анализов по каждому маркеру. "
                        "Раньше были видны только маркеры вне референса — здесь полный "
                        "список, с опциональным фильтром по названию маркера.",
        "parameters": {"type": "object", "properties": {
            "marker": {"type": "string", "description": "Фильтр по названию маркера (подстрока, необязательно)"},
            "limit": {"type": "integer", "description": "Максимум маркеров в ответе (по умолчанию 30)"},
        }},
        "executor": read_labs, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Read_Garmin_History",
        "description": "История сна/пульса/шагов из Garmin за произвольную глубину "
                        "(по умолчанию 30 дней) — раньше был виден только вчерашний день.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "Сколько дней истории вернуть (по умолчанию 30, максимум 180)"},
        }},
        "executor": read_garmin_history, "timeout": 4.0, "read_only": True,
    },
    {
        "name": "Record_Symptom",
        "description": "Запиши симптом в карту — новый или продолжение (тот же symptom_id, "
                        "если это та же тема). Вызывай ТОЛЬКО когда даёшь разбор, не на ходе "
                        "с уточняющими вопросами.",
        "parameters": {"type": "object", "properties": {
            "symptom_id": {"type": "string", "description": "Слаг латиницей, тот же при продолжении темы"},
            "symptom": {"type": "string"},
            "system": {"type": "string", "description": "ЖКТ|нервная|ССС|ОДА|кожа|эндокринная|общее"},
            "severity": {"type": "integer", "description": "1-10"},
            "status": {"type": "string", "enum": ["active", "monitoring", "resolved"]},
            "change": {"type": "string", "description": "появился|усилился|ослаб|без изменений|прошёл"},
            "domain": {"type": "string", "description": "gastro|neuro|cardio|endo|musculo|derm|psych|general"},
            "context": {"type": "string", "description": "что менялось в питании/активности/лекарствах"},
            "hypothesis": {"type": "string", "description": "гипотезы для врача, НЕ диагноз"},
            "notes": {"type": "string"},
        }, "required": ["symptom_id", "symptom"]},
        "executor": record_symptom, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Record_Note",
        "description": "Короткая клиническая заметка в карту.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"}, "note": {"type": "string"},
            "trigger": {"type": "string"}, "plan": {"type": "string"},
        }, "required": ["category", "note"]},
        "executor": record_note, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Open_Investigation",
        "description": "Открой расследование — только если через Read_Investigations "
                        "подтверждено, что открытых нет (лимит: одно одновременно).",
        "parameters": {"type": "object", "properties": {
            "inv_id": {"type": "string", "description": "Слаг латиницей"},
            "trigger": {"type": "string"}, "trigger_detail": {"type": "string"},
            "hypothesis": {"type": "string"},
        }, "required": ["inv_id", "trigger"]},
        "executor": open_investigation, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Update_Investigation",
        "description": "Обнови открытое расследование — findings ПОЛНОСТЬЮ (накопленная "
                        "сводка, не дельта), не только новую часть.",
        "parameters": {"type": "object", "properties": {
            "inv_id": {"type": "string"}, "findings": {"type": "string"},
            "hypothesis": {"type": "string"}, "questions_pending": {"type": "string"},
            "labs_suggested": {"type": "string"},
        }, "required": ["inv_id"]},
        "executor": update_investigation, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Close_Investigation",
        "description": "Закрой расследование с итоговым doctor_brief (7 пунктов, см. "
                        "системный промпт) — освобождает лимит на новое.",
        "parameters": {"type": "object", "properties": {
            "inv_id": {"type": "string"}, "findings": {"type": "string"},
            "doctor_brief": {"type": "string"}, "referral": {"type": "string"},
        }, "required": ["inv_id"]},
        "executor": close_investigation, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Plan_Lab",
        "description": "Запланируй пересдачу/новый анализ. Сначала проверь «УЖЕ "
                        "ЗАПЛАНИРОВАННЫЕ АНАЛИЗЫ» в досье — не вызывай, если тест там уже есть.",
        "parameters": {"type": "object", "properties": {
            "test": {"type": "string"}, "category": {"type": "string"},
            "interval_months": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": ["test"]},
        "executor": plan_lab, "timeout": 1.0, "read_only": False,
    },
    {
        "name": "Close_Recommendation",
        "description": "Закрой активную рекомендацию — например, пациент подтвердил, что "
                        "выполнил разовое действие (записался к врачу, сдал анализ). Ищет по "
                        "подстроке в названии; если совпадений несколько или ни одного — "
                        "вызов отклоняется, не гадает какую закрыть.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Слово/фраза из названия рекомендации, например 'кардиолог'"},
            "reason": {"type": "string", "description": "Почему закрываем — необязательно"},
        }, "required": ["title"]},
        "executor": close_recommendation, "timeout": 1.0, "read_only": False,
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOL_REGISTRY}


def openai_tool_schemas() -> list[dict]:
    """Формат для OpenRouter/OpenAI tool-calling (план §3.4)."""
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"],
        }}
        for t in TOOL_REGISTRY
    ]
