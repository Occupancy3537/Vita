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
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.ai_models import DEFAULT_MODEL
from app.dashboard import _num, _rows_as_dicts
from app.db import get_conn
from app import nutrition_telegram as telegram  # 2026-09-21: отчёты о питании -> @vvk_gemini_bot, не бот доктора (см. app/nutrition_telegram.py)
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
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

def build_daily_report(cur) -> Optional[dict]:
    now_vl = datetime.now(VL)
    start_vl = now_vl.replace(hour=0, minute=0, second=0, microsecond=0)
    meals = _fetch_meals(cur, start_vl, now_vl)
    if not meals:
        return None

    sums = {f: 0.0 for f in FIELDS}
    meal_lists = {"breakfast": [], "lunch": [], "snack": [], "dinner": [], "ultra": []}
    dates_seen = set()
    for m in meals:
        date_vl = m["Date"].astimezone(VL)
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
    result["Target_Date"] = now_vl.strftime("%Y-%m-%d")
    result["User_ID"] = TARGET_USER
    result["Breakfast_Meals"] = _format_meals(meal_lists["breakfast"])
    result["Lunch_Meals"] = _format_meals(meal_lists["lunch"])
    result["Snack_Meals"] = _format_meals(meal_lists["snack"])
    result["Dinner_Meals"] = _format_meals(meal_lists["dinner"])
    result["Ultra_Processed_Today"] = _format_meals(meal_lists["ultra"])
    result["season"] = SEASONS[now_vl.month - 1]
    result["month"] = now_vl.month
    return result


def build_daily_prompt(d: dict) -> str:
    return f"""
Ты — эксперт по превентивной медицине, биохакингу и активному долголетию. Твой подопечный: Влад, 43 года, живет во Владивостоке. Он уже проделал огромную работу над своим рационом, отлично понимает базу питания и строго контролирует добавленный сахар. Влад принимает витамин D3 2500. Учитывай, что холестерин из еды не считается вредным по последним исследованиям. Смотри на холестерин как на отягчающий фактор только при наличии большого количества насыщеных или трансжиров.
Сейчас {d['season']} (месяц {d['month']}).

Данные по питанию за сегодня:
Завтрак: {d['Breakfast_Meals']}
Обед: {d['Lunch_Meals']}
Перекус: {d['Snack_Meals']}
Ужин: {d['Dinner_Meals']}

Итоговые цифры по нутриентам: {json.dumps(d, ensure_ascii=False)}
(Применяй стандартные медицинские единицы измерения).

Твоя задача:

Оценка и похвала: Начни с позитива. Подсвети 2-3 сильные стороны сегодняшнего рациона. Влад молодец, дай ему позитивное подкрепление за его труд.

Адекватная аналитика: Найди 1 зону для улучшения. ВАЖНО: КАТЕГОРИЧЕСКИ ЗАПРЕЩАЕТСЯ ругать за высокий пищевой холестерин (яйца, морепродукты), если уровень насыщенных и трансжиров находится в пределах нормы. Фокусируйся на балансе жиров, а не на цифре общего холестерина.

Супер-лайфхаки: Дай один конкретный совет по сочетаемости съеденных сегодня блюд (что сработало круто, или как улучшить усвоение витаминов).

Анти-эйдж совет на завтра: Предложи ОДНО блюдо. ВАЖНО: каждый день блюдо должно быть РАЗНЫМ.

Ультра-обработанные продукты сегодня (NOVA 4): {d['Ultra_Processed_Today']}
ЭТО ОБЯЗАТЕЛЬНЫЙ ОТДЕЛЬНЫЙ АБЗАЦ, не смешивай его с похвалой или зоной для улучшения. Если список НЕ "Нет данных" — перечисли КАЖДОЕ блюдо/продукт из списка по имени. Поясняй, что именно делает продукт ультра-обработанным, ТОЛЬКО если это видно из самого названия/описания (явно указан промышленный ингредиент вроде маргарина/пальмового масла/консервантов/эмульгаторов, или это заведомо ультра-обработанная категория — снеки, газировка, колбаса, фастфуд, сухие завтраки). Если точный состав неизвестен (например, покупной продукт без описанного состава) — НЕ ПРИДУМЫВАЙ конкретные ингредиенты, которых нет в данных: честно скажи, что состав по одному названию не проверить, и предложи свериться с этикеткой, если для Влада это важно. Цель — чтобы Влад научился сам узнавать такую еду и в будущем её не брал, а не просто знал что "было плохо", и чтобы уверенность совета не превышала уверенность реальных данных. Тон спокойный, без нотаций. Если список — "Нет данных" (сегодня ультра-обработанного не было), не выдумывай пункт из ничего — одной короткой фразой отметь это как хороший знак.

Формат:
Начни с "Привет, Влад! 👋".
Пиши простым человеческим языком. Коротко, конкретно, 4-5 абзацев (с учётом нового абзаца про NOVA 4). Никаких сложных метафор, гипербол и заумных терминов. Без Markdown, используй эмодзи для структуры"""


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
    with get_conn() as conn, conn.cursor() as cur:
        d = build_daily_report(cur)
    if d is None:
        telegram.send_message(CHAT_ID, "⚠️ За сегодня не найдено записей о питании. Не забудь поесть и записать!")
        return

    text = call_model(build_daily_prompt(d), max_tokens=1800, reasoning_tokens=700, temperature=0.3)
    if text:
        telegram.send_message(CHAT_ID, text)

    with get_conn() as conn, conn.cursor() as cur:
        _write_day_sum(cur, d)
        conn.commit()
    _sync_nutrition_to_card(d)


# =====================================================================
# Недельный отчёт (порт n8n "Weekly Food Report", без промежуточного week_sum)
# =====================================================================

def build_weekly_report(cur) -> Optional[dict]:
    rows = _fetch_recent_meals(cur)
    if not rows:
        return None

    def shifted_date(dt):
        d = dt.astimezone(VL)
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
    with get_conn() as conn, conn.cursor() as cur:
        d = build_weekly_report(cur)
    if d is None:
        telegram.send_message(CHAT_ID, "⚠️ За последнюю неделю не найдено записей о питании.")
        return
    text = call_model(build_weekly_prompt(d), max_tokens=1800, reasoning_tokens=700, temperature=0.3)
    if text:
        telegram.send_message(CHAT_ID, text)


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
        return str(resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("nutrition_reports: вызов модели упал")
        return ""


def _sleep_until(hour: int, minute: int = 0, weekday: Optional[int] = None) -> None:
    now = datetime.now(VL)
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        days_ahead = (weekday - nxt.weekday()) % 7
        nxt += timedelta(days=days_ahead)
    if nxt <= now:
        nxt += timedelta(days=7 if weekday is not None else 1)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def run_daily_scheduler() -> None:
    logger.info("nutrition_reports daily scheduler: старт")
    while True:
        try:
            _sleep_until(DAILY_HOUR_VL, DAILY_MINUTE_VL)
            run_daily()
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
        except Exception as e:
            logger.exception("nutrition_reports run_weekly упал — повтор через неделю")
            alert_on_failure("nutrition_reports_weekly", e)
            time.sleep(3600)
