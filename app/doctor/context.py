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
"""
from typing import Optional

from psycopg import sql

from app import timeutil
from app.db import schema
from app.memory import get_context


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


def build_dossier(cur, text: str = "") -> dict:
    """Собирает всё досье одним проходом. Приёмка Phase 3: <300мс (план §4,
    шаг 3) — все запросы дешёвые (индексы/LIMIT), климат — единственный сетевой
    вызов, с коротким таймаутом и молчаливой деградацией."""
    return {
        "memory": get_context(cur, mode="question", payload={"text": text}),
        "garmin_yesterday": _garmin_yesterday(cur),
        "garmin_week_trend": _garmin_week_trend(cur),
        "nutrition_today": _nutrition_today(cur),
        "meals_today": _meals_today(cur),
        "active_meds": _active_meds(cur),
        "open_investigations": _open_investigations(cur),
        "recent_doctor_notes": _recent_doctor_notes(cur),
        "labs_out_of_range": _labs_out_of_range(cur),
        "planned_labs": _planned_labs(cur),
        "room_climate": _room_climate(cur),
        "recent_publications": _recent_publications(cur),
    }
