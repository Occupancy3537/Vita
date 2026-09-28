"""Порт n8n `Reports` (дневной путь — Daily Report) и `Weekly Food Report`
(2026-09-20, группа 2, последний из плановых LLM-порт для еды/самочувствия).

health.meals."Date" — timestamptz (не текст, как в Sheets-эру) — порт не
переносит JS-парсер дат (тот разбирал вперемешку ISO/DD.MM.YYYY/Excel-серийные
числа только из-за легаси Sheets), просто конвертирует средствами Python.

НЕ перенесено (сознательно): «Расчет_неделя»/«Запись_неделя» — Weekly-триггер
внутри самого `Reports`, писал лист `week_sum` (Nutrition-книга). Единственный
потребитель этого листа — `Weekly Food Report`, и при нормальной работе (одна
свежая строка week_sum в неделю) он просто суммирует и округляет эту ОДНУ
строку — то есть по факту ещё раз повторяет то же недельное усреднение
health.meals, которое уже посчитал `Расчет_неделя`. Порт считает недельное
среднее один раз, напрямую из health.meals, в момент отправки отчёта — тот же
принцип, что и весь остальной перенос (Postgres канон, лишний Sheets-хоп не
нужен, а не два прохода одного и того же расчёта).

Побочные находки при переносе, сохранены как есть, не тихо исправлены:
- `Weekly Food Report`'s Summarize-нода суммирует только 19 из 25 полей,
  которые считал `Расчет_неделя` (нет Витамин К/Е/Цинк/Холестерин/Вода) —
  порт (`WEEKLY_SURFACED_FIELDS`) отдаёт LLM то же более узкое подмножество,
  что видит прод сегодня, а не полный список — это не мой недосмотр, чужой.
- `Вода` в `Расчет_неделя`'s NUMERIC_FIELDS вообще не существует как колонка
  health.meals — эта сумма всегда была тождественно 0 и никогда не доходила
  до LLM (не входит и в узкое подмножество выше). Не переносил вообще —
  переносить нечего, эффекта на прод не было ни разу.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app import llm_usage
from app.ai_models import DEFAULT_MODEL
from app.dashboard import _num, _rows_as_dicts
from app.db import get_conn
from app import food_diary_telegram, notify
from app import run_log, timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
DAILY_HOUR_VL, DAILY_MINUTE_VL = 21, 45
WEEKLY_HOUR_VL = 12  # воскресенье, эмпирически по execution_entity (Weekly Food Report)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = DEFAULT_MODEL  # 2026-09-22: см. app/ai_models.py
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]
TARGET_USER = "Влад Васюк"

FIELDS = [
    "Calories", "Proteins", "Carbs", "Fats", "Магний", "Витамин D", "Омега-3 (EPA/DHA)",
    "Селен", "Йод", "Калий", "Железо", "Кальций", "Витамин B12", "Витамин К", "Витамин Е",
    "Цинк", "Клетчатка", "Холестерин", "Добавленный сахар", "Натрий", "Кофеин", "Алкоголь",
    "Насыщенные жиры", "Трансжиры",
]
WEEKLY_SURFACED_FIELDS = [
    "Calories", "Proteins", "Carbs", "Fats", "Магний", "Витамин D", "Омега-3 (EPA/DHA)",
    "Селен", "Йод", "Калий", "Железо", "Кальций", "Витамин B12", "Клетчатка",
    "Добавленный сахар", "Натрий", "Кофеин", "Алкоголь", "Насыщенные жиры", "Трансжиры",
]
SEASONS = ["зима", "зима", "весна", "весна", "весна", "лето", "лето", "лето", "осень", "осень", "осень", "зима"]


def _select_cols():
    return ", ".join(f'"{f}"' for f in FIELDS)


def _fetch_meals(cur, since_vl: datetime, until_vl: datetime) -> list[dict]:
    cur.execute(
        f'SELECT "Date", "Meal_description", "NOVA", {_select_cols()} FROM health.meals '
        'WHERE "User_ID" = %s AND "Date" >= %s AND "Date" < %s ORDER BY "Date"',
        (TARGET_USER, since_vl, until_vl),
    )
    return _rows_as_dicts(cur)


def _fetch_recent_meals(cur, limit_days: int = 60) -> list[dict]:
    """Для недельного отчёта — с запасом (сдвиг на 2ч, группировка по дню),
    без выгрузки ВСЕЙ истории health.meals (оригинал тянул её целиком)."""
    cur.execute(
        f'SELECT "Date", "Meal_description", "NOVA", {_select_cols()} FROM health.meals '
        'WHERE "User_ID" = %s AND "Date" >= now() - %s::interval ORDER BY "Date"',
        (TARGET_USER, f"{limit_days} days"),
    )
    return _rows_as_dicts(cur)


def _format_meals(items: list[str]) -> str:
    valid = [m for m in items if m and str(m).strip()]
    return "- " + "\n- ".join(valid) if valid else "Нет данных"


# =====================================================================
# Дневной отчёт (порт n8n "Reports" / Daily Report)
# =====================================================================

def build_daily_report(cur, for_date=None) -> Optional[dict]:
    """for_date=None (по умолчанию) — текущий, ЧАСТИЧНЫЙ день [00:00, сейчас) —
    для вечернего Telegram-отчёта в 21:45, как раньше. for_date=<дата> —
    ПОЛНЫЙ календарный день [00:00, следующий день 00:00) — использует
    finalize_yesterday() ниже (L6, аудит логики, 2026-09-23)."""
    if for_date is not None:
        start_vl = datetime.combine(for_date, datetime.min.time(), tzinfo=timeutil.person_tz())
        until_vl = start_vl + timedelta(days=1)
        target_date_str = for_date.isoformat()
    else:
        now_vl = timeutil.now_local()
        start_vl = now_vl.replace(hour=0, minute=0, second=0, microsecond=0)
        until_vl = now_vl
        target_date_str = now_vl.strftime("%Y-%m-%d")
    meals = _fetch_meals(cur, start_vl, until_vl)
    if not meals:
        return None

    sums = {f: 0.0 for f in FIELDS}
    meal_lists = {"breakfast": [], "lunch": [], "snack": [], "dinner": [], "ultra": []}
    dates_seen = set()
    for m in meals:
        date_vl = m["Date"].astimezone(timeutil.person_tz())
        dates_seen.add(date_vl.date())
        for f in FIELDS:
            sums[f] += _num(m.get(f)) or 0
        hour = date_vl.hour
        if 6 <= hour < 12:
            cat = "breakfast"
        elif 12 <= hour < 16:
            cat = "lunch"
        elif 18 <= hour < 23:
            cat = "dinner"
        else:
            cat = "snack"
        desc = m.get("Meal_description") or ""
        if desc:
            meal_lists[cat].append(desc)
        if _num(m.get("NOVA")) == 4 and desc:
            meal_lists["ultra"].append(desc)

    days = len(dates_seen) or 1
    result = {f: round(sums[f] / days) for f in FIELDS}
    result["daysTracked"] = days
    result["Target_Date"] = target_date_str
    result["User_ID"] = TARGET_USER
    result["Breakfast_Meals"] = _format_meals(meal_lists["breakfast"])
    result["Lunch_Meals"] = _format_meals(meal_lists["lunch"])
    result["Snack_Meals"] = _format_meals(meal_lists["snack"])
    result["Dinner_Meals"] = _format_meals(meal_lists["dinner"])
    result["Ultra_Processed_Today"] = _format_meals(meal_lists["ultra"])
    result["season"] = SEASONS[start_vl.month - 1]
    result["month"] = start_vl.month
    return result


def build_daily_prompt(d: dict) -> str:
    """«Досье — тонкое ядро; недельный разбор — дельта; эссе питания —
    подрезать» (2026-09-26, Часть 3): было ~1900 знаков, 4-5 абзацев (похвала,
    зона роста, лайфхак, анти-эйдж блюдо, отдельный абзац NOVA-4) — каждый
    день, независимо от того, есть ли реально что сказать. Мотивационную
    функцию (привычки Влада реально меняются от этих сообщений) — сохраняем;
    объём — нет: один факт дня числом + одна строка совета/смысла, максимум
    2-4 строки. Жёсткий лимит — в коде (_trim_to_lines в run_daily()), не
    только эта просьба в промпте."""
    return f"""Ты — эксперт по превентивной медицине для Влада (43 года, Владивосток, принимает витамин D3 2500, строго следит за добавленным сахаром). Холестерин из еды не считай вредным сам по себе — только при избытке насыщенных/трансжиров.

Данные по питанию за сегодня:
Завтрак: {d['Breakfast_Meals']}
Обед: {d['Lunch_Meals']}
Перекус: {d['Snack_Meals']}
Ужин: {d['Dinner_Meals']}
Итоговые цифры: {json.dumps(d, ensure_ascii=False)}

Ультра-обработанное сегодня (NOVA 4): {d['Ultra_Processed_Today']}

Твоя задача — ОДНО короткое сообщение, МАКСИМУМ 4 строки, каждая строка — законченная мысль:
1. Один факт дня ЧИСЛОМ, который реально заслуживает внимания (сильная сторона рациона или то, что выбилось) — с коротким позитивным или нейтральным комментарием.
2. Если сегодня было ультра-обработанное (NOVA 4, список не "Нет данных") — назови блюдо по имени одной строкой, без нотаций; поясняй, что делает его ультра-обработанным, только если это видно из названия, не выдумывай состав. Списка нет — не пиши эту строку вообще, не притягивай тему.
3. Один конкретный совет/лайфхак на сегодня-завтра (сочетаемость блюд, что съесть, усвоение витаминов) — если есть что сказать по делу, не ради формы.

Правила: без приветствий и вступлений, сразу по делу. КАЖДЫЙ пункт — С НОВОЙ СТРОКИ (реальный перенос строки между пунктами, не сплошной абзац). Простой человеческий язык, эмодзи можно, Markdown нельзя. Спокойная неделя без явных находок — одна строка похвалы за стабильность и всё, не выдумывай проблему из ничего. Никогда не больше 4 строк."""


DAILY_ESSAY_MAX_LINES = 4  # Часть 3 тикета «досье — тонкое ядро» (2026-09-26) —
# лимит в коде, не только просьба в промпте.
DAILY_ESSAY_MAX_CHARS = 500  # бэкстоп: живая проверка 2026-09-26 показала, что
# модель иногда пишет все 4 пункта ОДНИМ абзацем без переносов строк вообще —
# лимит по строкам сам по себе не гарантирует короткое сообщение, если "строк"
# физически одна; символьный кап работает независимо от того, расставила ли
# модель \n.


def _trim_to_lines(text: str, max_lines: int, max_chars: int = DAILY_ESSAY_MAX_CHARS) -> str:
    lines = [ln for ln in str(text or "").strip().split("\n") if ln.strip()]
    text = "\n".join(lines[:max_lines])
    if len(text) > max_chars:
        text = text[:max_chars - 1].rstrip() + "…"
    return text


# health.day_sum хранит спиртное под "Алкоголь, гр" (с единицей в имени
# колонки) — health.meals и весь остальной FIELDS-набор называют его просто
# "Алкоголь". Оригинальный n8n-инсёрт (`Запись_день PG`) уже делал это
# переименование явно в списке колонок INSERT — тут то же самое, не рассинхрон
# с моей стороны.
_DAY_SUM_COL = {f: f for f in FIELDS}
_DAY_SUM_COL["Алкоголь"] = "Алкоголь, гр"


def _write_day_sum(cur, d: dict) -> None:
    cols = ", ".join(f'"{_DAY_SUM_COL[f]}"' for f in FIELDS)
    placeholders = ", ".join("%s" for _ in FIELDS)
    updates = ", ".join(f'"{_DAY_SUM_COL[f]}" = EXCLUDED."{_DAY_SUM_COL[f]}"' for f in FIELDS)
    cur.execute(
        f'INSERT INTO health.day_sum ("User_ID", "Date", {cols}) '
        f'VALUES (%s, %s::date, {placeholders}) '
        f'ON CONFLICT ("Date") DO UPDATE SET "User_ID" = EXCLUDED."User_ID", {updates}',
        (d["User_ID"], d["Target_Date"], *[d[f] for f in FIELDS]),
    )


def _sync_nutrition_to_card(d: dict) -> None:
    """Порт "Sync Nutrition to Card" — раньше был HTTP-петлёй card-service ->
    card-service через свой же /facts/nutrition; теперь то же самое в одном
    процессе, локальный (отложенный) импорт — иначе цикл (main.py импортирует
    этот модуль для планировщика)."""
    from app.main import StructuredFact, _write_structured_facts

    day_iso = d["Target_Date"]
    facts = [
        StructuredFact(metric_key=f"nutrient:{f}", value_num=float(d[f]), ts_event=f"{day_iso}T00:00:00Z")
        for f in FIELDS if d.get(f) is not None
    ]
    if facts:
        try:
            _write_structured_facts(facts, origin="nutrition")
        except Exception:
            logger.exception("nutrition_reports: sync в card.fact упал (не критично)")


def run_daily() -> None:
    """2026-09-24 (тикет «раскладка ботов по тематическим чатам»): раньше
    (ROADMAP 5.5, тем же днём) копило через notify() и уходило второй секцией
    вечернего дайджеста — неудобно смешивать с остальным. Теперь шлёт САМ,
    напрямую в чат дневника питания (тот же бот, что и живая запись блюд —
    тематически логично), и только логирует факт в card.notify_log через
    notify.log_external_send (priority="normal", тот же контракт, что у
    doctor/intake.py::_deliver_emergency и app/doctor/anamnesis.py)."""
    with get_conn() as conn, conn.cursor() as cur:
        d = build_daily_report(cur)
    if d is None:
        food_diary_telegram.send_message(food_diary_telegram.CHAT_ID,
                                         "⚠️ За сегодня не найдено записей о питании. Не забудь поесть и записать!")
        notify.log_external_send("nutrition_reports", "normal")
        return

    text = call_model(build_daily_prompt(d), max_tokens=300, reasoning_tokens=200, temperature=0.3)
    text = _trim_to_lines(text, DAILY_ESSAY_MAX_LINES)
    if text:
        food_diary_telegram.send_message(food_diary_telegram.CHAT_ID, text)
        notify.log_external_send("nutrition_reports", "normal")

    with get_conn() as conn, conn.cursor() as cur:
        _write_day_sum(cur, d)
        conn.commit()
    _sync_nutrition_to_card(d)


def finalize_yesterday() -> None:
    """L6 (аудит логики, 2026-09-23, ВАЖНО): дневной отчёт выше пишет day_sum/
    card.fact по окну [00:00, 21:45) — блюдо, записанное ПОСЛЕ 21:45 (поздний
    ужин), не попадало никуда: не в сегодняшний прогон (уже прошёл к моменту
    записи), не в завтрашний (у него своё окно [00:00, 21:45) уже СЛЕДУЮЩЕГО
    дня) — тихая потеря данных навсегда, не разовая, каждый день.

    Досчитывает ВЧЕРАШНИЙ день целиком (00:00-24:00) и перезаписывает
    day_sum/card.fact тем же идемпотентным UPSERT (ON CONFLICT("Date")), что
    и обычный прогон. Вечерний Telegram-текст (LLM-комментарий за 21:45) НЕ
    переотправляется и не переписывается — это вечерний check-in про частичный
    день, ему положено быть по частичным данным; здесь исправляются только
    ХРАНИМЫЕ данные (day_sum/card.fact — источник для weekly-отчёта и истории),
    которые обязаны отражать день целиком.

    Вызывается из run_daily_scheduler() ПЕРЕД run_daily() каждого следующего
    дня — к этому моменту "вчера" уже полностью закончилось."""
    yesterday = timeutil.today() - timedelta(days=1)
    with get_conn() as conn, conn.cursor() as cur:
        d = build_daily_report(cur, for_date=yesterday)
    if d is None:
        return  # вчера не было записей — нечего досчитывать
    with get_conn() as conn, conn.cursor() as cur:
        _write_day_sum(cur, d)
        conn.commit()
    _sync_nutrition_to_card(d)

    # Vita v2, этап 1 (2026-09-28, Часть «Бэкенд-дельта» п.4): снимок дня для
    # экрана «вчера» — тот же момент, что и досчёт day_sum выше (вчера уже
    # точно закрыто). Импорт ленивый — тот же приём, что app/registrar.py
    # использует для app.main, здесь не строго обязателен (циклического
    # импорта нет), но держит nutrition_reports.py независимым от Vita на
    # уровне модуля, не только по смыслу. Сбой снимка не должен рвать день_сум
    # выше (уже записан) — отдельная транзакция, отдельный try.
    try:
        from app import vita
        with get_conn() as conn, conn.cursor() as cur:
            vita.write_day_snapshot(cur, yesterday)
            conn.commit()
    except Exception:
        logger.exception("finalize_yesterday: снимок дня Vita не записан — day_sum выше уже сохранён штатно")


# =====================================================================
# Недельный отчёт (порт n8n "Weekly Food Report", без промежуточного week_sum)
# =====================================================================

def build_weekly_report(cur) -> Optional[dict]:
    rows = _fetch_recent_meals(cur)
    if not rows:
        return None

    def shifted_date(dt):
        d = dt.astimezone(timeutil.person_tz())
        return (d - timedelta(days=1)).date() if d.hour < 2 else d.date()

    max_date = max(shifted_date(r["Date"]) for r in rows)
    window_start = max_date - timedelta(days=7)

    day_sums: dict = {}
    for r in rows:
        d = shifted_date(r["Date"])
        if not (window_start <= d < max_date):
            continue
        bucket = day_sums.setdefault(d, {f: 0.0 for f in FIELDS})
        for f in FIELDS:
            bucket[f] += _num(r.get(f)) or 0

    days = sorted(day_sums.keys())
    if not days:
        return None
    return {f: round(sum(day_sums[d][f] for d in days) / len(days)) for f in WEEKLY_SURFACED_FIELDS}


def build_weekly_prompt(d: dict) -> str:
    return f"""Ты — эксперт по превентивной медицине и долголетию. Проанализируй данные о питании за неделю для 43-летнего мужчины из Владивостока.

Данные за неделю: {json.dumps(d, ensure_ascii=False)} Единицы измерения: Калории (ккал), БЖУ (граммы), Минералы и Омега-3(мг), Витамины (мкг).

Контекст региона: Владивосток — город с высокой солнечной активностью зимой, но холодными ветрами. Учти доступность морепродуктов и специфику региона.
Твоя задача:
Сравни суммы нутриентов с нормой для 43-летнего мужчины (акцент на тестостерон, здоровье сосудов и суставов).
По витамину D: учитывай солнце, но предупреди, если суммы из еды слишком малы для зимы. Пациент принимает витамин D 2500 ед.
По йоду: не делай акцент на дефиците (регион обеспечен), но проверь баланс других минералов (Магний, Селен).
Дай 3 конкретных совета по питанию на следующую неделю для продления активного долголетия.
Все значения уже приведены к среднему за день.
ВАЖНО: Пиши простым текстом без использования Markdown (без звездочек, решеток и жирного шрифта). Используй абзацы и эмодзи.
Структура ответа:
1. Короткий итог (1-2 предложения)
2. Где дефициты
3. Где перебор
4. 3 конкретных действия на неделю.
ОГРАНИЧЕНИЕ ДЛИНЫ: Твой ответ должен быть кратким и укладываться в 3500 символов. Пиши только самое важное, без "воды"."""


def run_weekly() -> None:
    """2026-09-24 (ROADMAP 5.5): source отдельный от run_daily() —
    "недельный отчёт в свой день" в дайджесте, не вытесняет и не сливается
    со "сводкой питания" (2-й фиксированный блок дайджеста)."""
    with get_conn() as conn, conn.cursor() as cur:
        d = build_weekly_report(cur)
    if d is None:
        notify.notify("nutrition_reports_weekly", "normal", "⚠️ За последнюю неделю не найдено записей о питании.")
        return
    text = call_model(build_weekly_prompt(d), max_tokens=1800, reasoning_tokens=700, temperature=0.3)
    if text:
        notify.notify("nutrition_reports_weekly", "normal", text)


# =====================================================================
# LLM + планировщики
# =====================================================================

def call_model(prompt: str, max_tokens: int, reasoning_tokens: int, temperature: float, timeout: float = 30.0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return ""
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL, "temperature": temperature, "max_tokens": max_tokens,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "reasoning": {"max_tokens": reasoning_tokens},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        llm_usage.record("nutrition_reports", MODEL, data.get("usage"))
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("nutrition_reports: вызов модели упал")
        return ""


def _sleep_until(hour: int, minute: int = 0, weekday: Optional[int] = None) -> None:
    """Фаза 3 (2026-09-22): сон до часа ПО ПОЯСУ ЧЕЛОВЕКА (timeutil), кусками
    по 10 минут — переключение /tz подхватывается без ожидания следующего дня."""
    timeutil.sleep_until_local(hour, minute, weekday=weekday)


def run_daily_scheduler() -> None:
    logger.info("nutrition_reports daily scheduler: старт")
    while True:
        try:
            _sleep_until(DAILY_HOUR_VL, DAILY_MINUTE_VL)
            # L6: досчитать ВЧЕРАШНИЙ день целиком (см. finalize_yesterday) ДО
            # сегодняшнего частичного отчёта — порядок не важен для сегодняшних
            # данных (разные даты), но так оба шага логически в одном месте.
            # Своя защита от сбоя: не должна блокировать сегодняшний отчёт.
            try:
                finalize_yesterday()
            except Exception:
                logger.exception("nutrition_reports: finalize_yesterday упал — сегодняшний отчёт всё равно идёт")
            run_daily()
            run_log.mark_run("nutrition_reports_daily")
        except Exception as e:
            logger.exception("nutrition_reports run_daily упал — повтор завтра")
            alert_on_failure("nutrition_reports_daily", e)
            time.sleep(3600)


def run_weekly_scheduler() -> None:
    logger.info("nutrition_reports weekly scheduler: старт")
    while True:
        try:
            _sleep_until(WEEKLY_HOUR_VL, 0, weekday=6)  # 6 = воскресенье (Python Monday=0)
            run_weekly()
            run_log.mark_run("nutrition_reports_weekly")
        except Exception as e:
            logger.exception("nutrition_reports run_weekly упал — повтор через неделю")
            alert_on_failure("nutrition_reports_weekly", e)
            time.sleep(3600)
