"""
Досье пациента для одного хода (план §3.3, §3.9): `get_context()` (П4 — браслет,
горячий слой, retrieval по вопросу) + срезы схемы `health` напрямую через
Postgres — Garmin, сегодняшнее питание, активные препараты, открытые
расследования, последние заметки врача, лабы вне референса, комнатный климат.

2026-09-20 (#28): комнатный климат был последним живым мостом в n8n (план
§2.3 сознательно его оставлял — там реально нужны были Google-креды) — теперь
читается из health.microclimate (зеркало того же листа, ночной
sheets_to_pg_mirror.js), ни одного обращения к n8n в этом модуле не осталось.

Раньше (старый доктор) это собиралось из 9+ Sheets-инструментов, вызываемых
моделью ПО ЖЕЛАНИЮ — не гарантия, что она вообще спросит. Здесь всё это —
СТАТИЧЕСКАЯ часть промпта, всегда собранная заранее (то же решение, что уже
принято для сегодняшних приёмов пищи и климата в докторе на n8n, см. память
`ai-agent-tool-call-doubles-latency`: любой инструмент-вызов добавляет целый
лишний проход модели, а эти данные релевантны почти всегда).

«Досье — тонкое ядро» (2026-09-26): статическая часть выросла до 13 блоков
(~12 900 знаков) каждый ход, независимо от темы сообщения — при том что у
доктора есть 22 инструмента, которыми он может дозапросить то, чего не хватает.
build_dossier() теперь собирает ПОСТОЯННОЕ ЯДРО (мед. ограничения/гейт,
активные препараты, вчерашний Garmin, активные проблемы/расследования — то,
что относится к разговору почти всегда) + 1-3 блока из оставшихся 10,
которые выбирает route_blocks() детерминированно по ключевым словам сообщения
(без LLM — дешевле и предсказуемее, чем звать модель ради маршрутизации).
Тема не распознана -> только ядро, инструменты остаются главным путём к
деталям, досье их больше не дублирует заранее "на всякий случай".

Красные флаги (app/redflag*.py, app/doctor/gate.py) читают только текст
сообщения и не проходят через build_dossier() вообще — их независимость от
состава/объёма досье не меняется этой правкой ни на строку.
"""
import re
from typing import Optional

from psycopg import sql

from app import timeutil
from app.db import schema
from app.memory import get_context
from app.patient_gate import load_gate


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _garmin_yesterday(cur) -> Optional[dict]:
    """Последняя строка health.daily_trends — куратированный набор полей, не все
    37+ колонок (что реально влияет на разговор о самочувствии, не весь Garmin-дамп)."""
    cur.execute(
        'SELECT "Дата", "Чистый_сон_мин", "Эффективность_сна_", "Пульс_ночной_средний", '
        '"Оценка_сна_балл", "SpO2_ночь_среднее", "Шаги_за_вчера", "Тренировка_Ккал", '
        '"Окно_голода_до_сна_ч", "Лекарства_принимаемые" '
        "FROM health.daily_trends ORDER BY \"Дата\" DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return None
    (date, sleep_min, sleep_eff, rhr, sleep_score, spo2, steps, training_kcal,
     fasting_h, meds_seen) = row
    return {
        "date": str(date),
        "sleep_min": _num(sleep_min),
        "sleep_efficiency_pct": _num(sleep_eff),
        "resting_hr": _num(rhr),
        "sleep_score": _num(sleep_score),
        "spo2_avg": _num(spo2),
        "steps": _num(steps),
        "training_kcal": _num(training_kcal),
        "fasting_before_sleep_h": _num(fasting_h),
        "meds_seen_in_garmin_note": meds_seen or None,
    }


def _garmin_week_trend(cur, days: int = 7) -> dict:
    """Средние за последние N дней — не для диагностики, для "стало хуже/лучше,
    чем обычно" в разговоре, тот же принцип, что и Health Watchdog (2b, сдвиг к
    границе референса)."""
    cur.execute(
        'SELECT "Чистый_сон_мин", "Пульс_ночной_средний", "Шаги_за_вчера", "Оценка_сна_балл" '
        'FROM health.daily_trends ORDER BY "Дата" DESC LIMIT %s',
        (days,),
    )
    rows = cur.fetchall()
    if not rows:
        return {"days": 0}

    def avg(idx):
        vals = [_num(r[idx]) for r in rows]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "days": len(rows),
        "avg_sleep_min": avg(0),
        "avg_resting_hr": avg(1),
        "avg_steps": avg(2),
        "avg_sleep_score": avg(3),
    }


def _nutrition_today(cur) -> Optional[dict]:
    cur.execute(
        'SELECT "Calories", "Proteins", "Carbs", "Fats", "Кофеин", "Алкоголь, гр", '
        '"Добавленный сахар" FROM health.day_sum '
        "WHERE \"Date\" = (now() AT TIME ZONE %s)::date",
        (timeutil.person_tz_name(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    kcal, protein, carbs, fats, caffeine, alcohol, sugar = row
    return {
        "kcal": _num(kcal), "protein_g": _num(protein), "carbs_g": _num(carbs),
        "fats_g": _num(fats), "caffeine_mg": _num(caffeine),
        "alcohol_g": _num(alcohol), "added_sugar_g": _num(sugar),
    }


def _meals_today(cur) -> list[dict]:
    tz = timeutil.person_tz_name()
    cur.execute(
        "SELECT to_char(\"Date\" AT TIME ZONE %s, 'HH24:MI') AS t, "
        '"Meal_description", "Calories", "Proteins", "Fats", "Carbs" '
        'FROM health.meals '
        "WHERE (\"Date\" AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date "
        'ORDER BY "Date"',
        (tz, tz, tz),
    )
    return [
        {"time": t, "description": desc, "kcal": _num(k), "protein_g": _num(p),
         "fats_g": _num(f), "carbs_g": _num(c)}
        for t, desc, k, p, f, c in cur.fetchall()
    ]


def _active_meds(cur) -> list[dict]:
    cur.execute(
        sql.SQL(
            "SELECT name, dose, regimen, kind FROM {t} WHERE status = 'active' ORDER BY started_ts"
        ).format(t=sql.Identifier(schema(), "intervention"))
    )
    return [{"name": n, "dose": d, "regimen": r, "kind": k} for n, d, r, k in cur.fetchall()]


def _open_investigations(cur) -> list[dict]:
    cur.execute(
        "SELECT inv_id, trigger, hypothesis, status, opened FROM health.investigations "
        "WHERE lower(status) = 'open' ORDER BY opened DESC"
    )
    return [
        {"inv_id": i, "trigger": t, "hypothesis": h, "status": s, "opened": str(o)}
        for i, t, h, s, o in cur.fetchall()
    ]


def _recent_doctor_notes(cur, limit: int = 5) -> list[dict]:
    cur.execute(
        "SELECT to_char(note_date, 'YYYY-MM-DD') AS d, category, note "
        "FROM health.doctor_notes ORDER BY note_date DESC LIMIT %s",
        (limit,),
    )
    return [{"date": d, "category": c, "note": n} for d, c, n in cur.fetchall()]


def _labs_out_of_range(cur, limit: int = 10) -> list[dict]:
    """Самая свежая запись по каждому marker_key, только если вне референса —
    "лабы вне референса" из инвентаря старого доктора (§1 п.5), не полный дамп
    257 строк в промпт каждый ход."""
    cur.execute(
        sql.SQL(
            "SELECT DISTINCT ON (marker_key) marker_key, marker_label, value_num, unit, "
            "ref_min, ref_max, ts_event FROM {t} "
            "WHERE value_num IS NOT NULL AND (ref_min IS NOT NULL OR ref_max IS NOT NULL) "
            "ORDER BY marker_key, ts_event DESC"
        ).format(t=sql.Identifier(schema(), "lab_result"))
    )
    out = []
    for key, label, value, unit, lo, hi, ts in cur.fetchall():
        lo_f, hi_f, val_f = (float(lo) if lo is not None else None,
                              float(hi) if hi is not None else None, float(value))
        out_of_range = (lo_f is not None and val_f < lo_f) or (hi_f is not None and val_f > hi_f)
        if out_of_range:
            out.append({"marker": label or key, "value": val_f, "unit": unit,
                        "ref_min": lo_f, "ref_max": hi_f, "date": str(ts)[:10]})
    return out[:limit]


def _planned_labs(cur, limit: int = 15) -> list[dict]:
    """2026-09-19: до этого health.lab_plan нигде не читался доктором вообще —
    Plan_Lab был write-only чёрным ящиком. Реальный итог: за 2 отдельных
    разговора доктор дважды спланировал пересдачу B12 (17.09 и 18.09), не имея
    возможности узнать про первую запись. Правильный фикс — не жёсткое правило
    "не дублируй B12" (это не масштабируется на другие анализы), а видимость:
    досье как и остальные разделы (open_investigations, active_meds)."""
    cur.execute(
        "SELECT \"Plan_ID\", \"Test\", \"Category\", \"Next_Due\", \"Reason\", \"Source\" "
        "FROM health.lab_plan WHERE lower(coalesce(\"Status\", 'active')) = 'active' "
        "ORDER BY \"Next_Due\" NULLS LAST LIMIT %s",
        (limit,),
    )
    return [
        {"plan_id": pid, "test": t, "category": cat, "next_due": due, "reason": r, "source": src}
        for pid, t, cat, due, r, src in cur.fetchall()
    ][:limit]


def _room_climate(cur) -> Optional[dict]:
    """2026-09-20 (#28): был единственным мостом в n8n (план §2.3, room-climate-
    now) — n8n-вебхук читал Google Sheets синхронно на каждый запрос (~1.6с,
    план оценивал в 83мс — разошлось на порядок, отсюда и был кэш на 10 мин).
    health.microclimate теперь зеркалируется из того же листа ночным
    sheets_to_pg_mirror.js (пишет его сенсор Яндекс.Дома, раз в час, эту
    сторону не переносим) — обычный SELECT по PK не нуждается в кэше и не
    может "устареть между обновлениями сенсора" сильнее, чем сама таблица."""
    cur.execute('SELECT "Температура", "Влажность", "PM2.5", "Дата" FROM health.microclimate ORDER BY "Дата" DESC LIMIT 1')
    row = cur.fetchone()
    if row is None:
        return None
    temp, hum, pm25, measured_at = row
    return {"temp_c": _num(temp), "humidity_pct": _num(hum), "pm25": _num(pm25), "measured_at": measured_at}


def _recent_publications(cur, limit: int = 5) -> list[dict]:
    """«Научный контур» (2026-09-25, Часть 5.1) — свежие релевантные публикации
    по темам профиля, чтобы доктор мог сослаться на конкретную работу в разговоре,
    не выдумывая источник. grade — структурный (design_type/phase из card.publication,
    не из этого запроса и не от LLM здесь), why — та же строка, что попала в дайджест.

    2026-09-25 (живая проверка тем же днём): простое "top-N по ts_recorded" на
    скане из 10 тем показывало 5 позиций ИЗ ОДНОЙ темы (какая сканировалась
    последней) — вопрос про L5/S1 не находил в досье ни одной публикации про
    L5/S1, доктор был вынужден 3 раунда подряд звать Search_Publications вместо
    ответа по досье. DISTINCT ON (topic_key) — по одной, самой свежей, публикации
    С КАЖДОЙ темы, чтобы досье покрывало темы, а не последнюю по времени скана."""
    cur.execute(
        sql.SQL(
            "SELECT title, design_type, phase, why_for_you, url FROM ("
            "  SELECT DISTINCT ON (topic_key) title, design_type, phase, why_for_you, url, ts_recorded "
            "  FROM {t} WHERE relevant = true ORDER BY topic_key, ts_recorded DESC"
            ") per_topic ORDER BY ts_recorded DESC LIMIT %s"
        ).format(t=sql.Identifier(schema(), "publication")),
        (limit,),
    )
    return [
        {"title": title, "design_type": design_type, "phase": phase, "why": why, "url": url}
        for title, design_type, phase, why, url in cur.fetchall()
    ][:limit]


def _anomaly_dispositions(cur, limit: int = 10) -> list[dict]:
    """«Мост аномалия -> действие» (2026-09-25, Часть 3.3: "история диспозиций
    видна доктору — 'что я уже говорил про HRV'") — переиспользует
    app/anomaly_disposition.py::recent_dispositions (не дублирует SQL); контекст
    для генерации гипотез инструментом Dispose_Anomaly, не отдельная запись."""
    from app.anomaly_disposition import recent_dispositions
    return recent_dispositions(cur, limit=limit)


def _recent_consilium_summaries(cur, limit: int = 3) -> list[dict]:
    """Консилиум специалистов (2026-09-25, Часть 4.3) — "чтобы доктор знал,
    что уже решено коллегами", не переспрашивал заново."""
    cur.execute(
        sql.SQL("SELECT topic, ts_recorded::date, actions, status FROM {t} "
                "ORDER BY ts_recorded DESC LIMIT %s").format(t=sql.Identifier(schema(), "consilium_report")),
        (limit,),
    )
    return [{"topic": t, "date": str(d), "actions": a, "status": s} for t, d, a, s in cur.fetchall()]


def _gate_status(cur) -> dict:
    """Медограничения/гейт нагрузки — ЯДРО досье (Часть 1.1). Переиспользует
    app.patient_gate.load_gate — ту же единственную реализацию, что уже
    используют dashboard.py и weekly_advisor.py (П3, не третья независимая
    копия gate-логики). blocked=False -> остальные поля не нужны, format_dossier
    просто не покажет секцию."""
    cur.execute(
        'SELECT "Status", "Contra_Load", "Condition", "Allowed", "Provokers", '
        '"Review_Due", "Source", "Confirmed_Date" FROM health.patient_state'
    )
    cols = ["Status", "Contra_Load", "Condition", "Allowed", "Provokers", "Review_Due", "Source", "Confirmed_Date"]
    pstate = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute('SELECT "ОДА и неврология" FROM health.user_profile LIMIT 1')
    row = cur.fetchone()
    profile = {"ОДА и неврология": row[0]} if row else {}
    gate = load_gate(pstate, profile)
    if not gate.get("blocked"):
        return {"blocked": False}
    return {"blocked": True, "condition": gate.get("condition"), "contra": gate.get("contra"),
            "allowed": gate.get("allowed")}


def _active_problems(cur) -> list[dict]:
    """Активные проблемы (темы разбора длиной в несколько эпизодов) — ЯДРО
    досье (Часть 1.1). Тот же запрос, что app/consilium.py::_active_problems
    (card.problem) — не импортируем оттуда: app/doctor/ в этом тикете
    ограничен context.py, consilium.py не трогаем и не тянем из него
    внутренности ради одного SELECT."""
    cur.execute(
        sql.SQL("SELECT id, title, icd_hint, opened_ts::date FROM {t} "
                "WHERE status = 'active' ORDER BY opened_ts DESC").format(t=sql.Identifier(schema(), "problem"))
    )
    return [{"id": i, "title": t, "icd_hint": icd, "opened": str(o)} for i, t, icd, o in cur.fetchall()]


# =====================================================================
# Роутер (Часть 1.2) — детерминированный, по ключевым словам, без LLM
# =====================================================================

_ROUTER_RULES: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"болит|боль|тошнит|тошнот|температур|сыпь|кружится|голова.{0,6}кругом|"
                r"онеме|отёк|отек|плохо себя чувств|симптом|обостр|знобит|слабост", re.I),
     ("recent_doctor_notes", "room_climate", "anomaly_dispositions", "garmin_week_trend")),
    (re.compile(r"добавк|витамин|препарат|лекарств|дозиров|таблетк|можно ли (пить|принимать)|совместим", re.I),
     ("labs_out_of_range", "recent_publications")),
    (re.compile(r"\bем\b|\bел\b|\bела\b|поел|поела|\bеда\b|питани|калори|белк[а-и]|углевод|жир[а-ы]|"
                r"диет|рацион|перекус|позавтракал|пообедал|поужинал", re.I),
     ("nutrition_today", "meals_today")),
    (re.compile(r"анализ|лаборатор|кровь сдал|биохими|результат.{0,10}анализ", re.I),
     ("labs_out_of_range", "planned_labs")),
    (re.compile(r"исследован|статья|публикац|наука|изучен|доказательств", re.I),
     ("recent_publications",)),
    (re.compile(r"консилиум|что решили специалист|мнение специалист", re.I),
     ("recent_consilium_summaries",)),
]


def route_blocks(text: str) -> set[str]:
    """Часть 1.2: выбирает 0-неск. дополнительных блоков досье по ключевым
    словам сообщения. Правила МОГУТ пересекаться (сообщение и про симптом, и
    про еду) — объединяем совпадения, не выбираем одну тему произвольно.
    Тема не распознана -> пустое множество -> досье = только ядро."""
    text = text or ""
    blocks: set[str] = set()
    for rx, names in _ROUTER_RULES:
        if rx.search(text):
            blocks.update(names)
    return blocks


_ROUTABLE_BUILDERS = {
    "garmin_week_trend": _garmin_week_trend,
    "nutrition_today": _nutrition_today,
    "meals_today": _meals_today,
    "recent_doctor_notes": _recent_doctor_notes,
    "labs_out_of_range": _labs_out_of_range,
    "planned_labs": _planned_labs,
    "room_climate": _room_climate,
    "recent_publications": _recent_publications,
    "anomaly_dispositions": _anomaly_dispositions,
    "recent_consilium_summaries": _recent_consilium_summaries,
}


def build_dossier(cur, text: str = "") -> dict:
    """Ядро (5 блоков, всегда) + до 10 маршрутизируемых блоков по теме
    сообщения (Часть 1.1-1.2). Приёмка Phase 3: <300мс (план §4, шаг 3) — все
    запросы дешёвые (индексы/LIMIT), климат — единственный сетевой вызов, с
    коротким таймаутом и молчаливой деградацией; маршрутизация выбирает НЕ
    более 4 доп. запросов даже при пересечении всех правил."""
    dossier = {
        "memory": get_context(cur, mode="question", payload={"text": text}),
        "gate_status": _gate_status(cur),
        "active_problems": _active_problems(cur),
        "active_meds": _active_meds(cur),
        "garmin_yesterday": _garmin_yesterday(cur),
        "open_investigations": _open_investigations(cur),
    }
    for name in route_blocks(text):
        dossier[name] = _ROUTABLE_BUILDERS[name](cur)
    return dossier
