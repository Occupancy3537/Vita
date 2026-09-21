"""Порт n8n `_System Check` (SystemCheck01) в card-service — 2026-09-19.

Причина переноса именно этого воркфлоу первым (не по алфавиту, по фактам):
это самый прожорливый по памяти узел из всех активных — `Get Daily_Trends`/
`Get day_sum` в оригинале делали `SELECT d.* ... ORDER BY` БЕЗ фильтра по дате
и без LIMIT, то есть каждое утро тянули ВСЮ историю обеих таблиц целиком в
память одной Code-ноды. Это и стало прямой причиной двух OOM-инцидентов подряд
(18.09: сначала "Node ran out of memory" в самом узле Check, затем упал весь
главный процесс n8n — см. STATE.md/AGENT_SYNC.md #15, #20). Полечили как
временную меру (лимит памяти раннера, потом лимит V8-кучи), но структурная
причина — сам паттерн неограниченного чтения — оставалась. Порт в Python
читает те же таблицы, но с фильтром на последние 5-10 дней, как и было
задумано логикой самой проверки (она и раньше смотрела только на последние
5-10 дней, просто через инструмент, который для этого тащил вообще всё).

Не перенесено (сознательно, не забыто):
- Сверка Sheets-версии Daily_Trends/day_sum против Postgres (dual-write check) —
  эта проверка была нужна на переходный период миграции Sheets→PG, который уже
  закрыт; Postgres теперь единственный источник для этих данных в самой этой
  проверке, сверять не с чем.
- Свежесть Recommendations_Log — с переносом Weekly AI Advisor (2026-09-20,
  #33) таблица теперь в Postgres и технически доступна отсюда, но отдельную
  проверку свежести пока не строил — сам советник (app/weekly_advisor.py)
  падает в Telegram явным предупреждением, если модель не ответила или блок
  действий не распарсился, так что немой сбой самому себе он не устроит;
  отдельная internal-проверка «когда была последняя запись» — не сегодняшняя
  задача, можно добавить позже, если понадобится.
- Дубли по дате в Daily_Trends/day_sum — в Postgres дата это PRIMARY KEY,
  дубликат структурно невозможен (ON CONFLICT/PK не даст вставить); в Sheets-
  версии эта проверка была нужна ровно потому, что Sheets такого не гарантирует.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from app.db import get_conn
from app import hermes_telegram as telegram  # 2026-09-21: алерты -> Hermes, не бот доктора (см. app/hermes_telegram.py)
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
CHECK_HOUR_VL = 8
CHECK_MINUTE_VL = 43

# 2026-09-21 (AGENT_SYNC #38/#42, независимый аудит ZCode): n8n больше не
# существует вообще (Влад: «n8n вообще не нужен», контейнер удалён). Раньше
# здесь были _check_dashboard_widget() (бил в n8n-вебхук /webhook/dashboard —
# 502 каждое утро, ложное "⚠️ проблемы" на компонент, которого больше нет) и
# _check_n8n_active() (n8n REST API + захардкоженный JWT-ключ n8n) — обе
# функции и константы удалены целиком вместе с проверяемым объектом, не
# заменены другими: сам n8n больше не часть системы, проверять нечего.
#
# (label, path) — path без токена: экраны переезжают на card-service по одному
# (bioage — 2026-09-19, #22), у каждого свой адрес (localhost, не путь у
# n8n). Раньше третьим элементом был max_age_hours — эти три эндпоинта
# теперь считают ответ ЗАНОВО на каждый вызов (не кэш) и сами ставят
# updated_at=now() в момент ответа, поэтому проверка "возраст < N часов"
# структурно не может провалиться — убрана (была источником ложного "кэши
# свежие" в тексте "всё ок", хотя проверялось только "эндпоинт вообще
# ответил"). Реальная свежесть ДАННЫХ (не кэша) проверяется отдельно —
# _check_gaps()/_check_anomaly_freshness() смотрят в сами таблицы Postgres.
# 2026-09-21 (#38/#45, аудит "секреты в коде"): токен раньше был хардкожен
# ЛИТЕРАЛОМ в каждом из трёх URL — теперь читается один раз из DASHBOARD_TOKEN
# (тот же env, что уже проверяет сам /dashboard/*) и подставляется в _check_webhooks().
WEBHOOK_CHECKS = [
    ("today-dashboard", "http://127.0.0.1:8080/dashboard/today"),
    ("bioage-dashboard", "http://127.0.0.1:8080/dashboard/bioage"),
    ("weekly-nutrients", "http://127.0.0.1:8080/dashboard/weekly-nutrition"),
]
# "recipes" убран отсюда 2026-09-20 (по прямому запросу Влада — "тратит токены
# впустую"): воркфлоу «Вычисление дефицитов для рекомендации рецептов» ни разу
# не читается фронтендом (grep v4.html/index.html — пусто), последний реальный
# прогон 07.09, при этом висел активным AI Agent-узлом на Schedule Trigger.
# Заодно нашлась и починена настоящая находка (#26): webhook_entity держал
# регистрацию ЖИВОЙ даже при active=0 в workflow_entity — обратный случай
# уже знакомого "raw-SQL-активация не перерегистрирует вебхуки", только здесь
# сама деактивация (сделанная кем-то раньше не через REST API) не сняла
# регистрацию. Почищено циклом activate→deactivate через REST API — второй
# вызов заставил n8n пересобрать реестр вебхуков и снять его по-настоящему
# (проверено: старый /webhook/recipes теперь 404).

# EXPECTED_ACTIVE_N8N / _check_n8n_active() — удалены 2026-09-21 (#38/#42):
# n8n больше не существует, проверять активные воркфлоу не у чего. История
# этого списка (постепенное опустошение по мере переноса каждого воркфлоу
# на card-service, #21-#37) — в git log этого файла и AGENT_SYNC.md.


def _d10(v) -> str:
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v or "")[:10]


def _vl_now() -> datetime:
    return datetime.now(VL)


def _check_webhooks(problems: list, notes: list) -> dict:
    """Возвращает ответ today-dashboard (нужен ниже для проверки гейта) —
    та же экономия одного лишнего запроса, что была в оригинале.

    2026-09-21 (#38/#42): проверка возраста updated_at/computed_at УБРАНА —
    эти три эндпоинта считают ответ заново на каждый вызов (не кэш) и сами
    ставят метку времени в момент ответа, поэтому "возраст < порога"
    структурно не мог провалиться никогда — ложная гарантия "кэши свежие"
    в тексте отчёта. Реальная проверка (эндпоинт вообще отвечает, без
    ошибки в теле) осталась; свежесть самих ДАННЫХ смотрят _check_gaps()/
    _check_anomaly_freshness() напрямую в Postgres."""
    today_cache = None
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    for path, base_url in WEBHOOK_CHECKS:
        url = f"{base_url}?token={dashboard_token}"
        try:
            r = httpx.get(url, timeout=20.0)
            r.raise_for_status()
            res = r.json()
        except Exception as e:
            problems.append(f"{path}: не отвечает ({str(e)[:60]})")
            continue
        if path == "today-dashboard":
            today_cache = res
        if isinstance(res, dict) and res.get("error"):
            problems.append(f"{path}: error={res['error']}")
    return today_cache or {}


def _missing_dates(cur, table: str, date_col: str, days_back: int, notes: list, label: str) -> set:
    since = (_vl_now() - timedelta(days=days_back)).date()
    cur.execute(f'SELECT "{date_col}" FROM health.{table} WHERE "{date_col}" >= %s', (since,))
    rows = cur.fetchall()
    got = {_d10(r[0]) for r in rows}
    if not got:
        notes.append(f"{label} не прочитан за последние {days_back} дн — проверка дыр пропущена")
        return set()
    expected = {(_vl_now() - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(2, min(days_back, 6) + 1)}
    return expected - got


def _check_gaps(cur, problems: list, notes: list) -> None:
    m = _missing_dates(cur, "daily_trends", "Дата", 6, notes, "Daily_Trends")
    if m:
        problems.append(f"Daily_Trends нет строк за: {', '.join(sorted(m))} (Garmin-пайплайн?)")
    m = _missing_dates(cur, "day_sum", "Date", 6, notes, "day_sum")
    if m:
        problems.append(f"day_sum нет строк за: {', '.join(sorted(m))} (лог еды?)")

    since = (_vl_now() - timedelta(days=10)).date()
    cur.execute(
        "SELECT DISTINCT (\"Date\" AT TIME ZONE 'Asia/Vladivostok')::date FROM health.meals "
        "WHERE (\"Date\" AT TIME ZONE 'Asia/Vladivostok')::date >= %s", (since,),
    )
    meal_dates = {_d10(r[0]) for r in cur.fetchall()}
    if not meal_dates:
        notes.append("Meals не прочитан — проверка дыр пропущена")
    else:
        expected = {(_vl_now() - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(2, 7)}
        m = expected - meal_dates
        if m:
            problems.append(f"Meals нет записей за: {', '.join(sorted(m))} (лог еды не пишется?)")


def _check_pg_status(cur, problems: list) -> None:
    cur.execute(
        "SELECT (SELECT max(\"Дата\") FROM health.daily_trends), "
        "(SELECT max(date) FROM health.phenoage_log), "
        "(SELECT count(*) FROM health.investigations WHERE status IN ('open','report_ready'))"
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        problems.append("🔴 Postgres (health) не отвечает или health.daily_trends пуст — проверь контейнер pg / сеть pgnet")


# Премортем (2026-09-20, задача Влада "давай сделаем 1,3,4,5,7", проблема #1
# "тихая эрозия данных через накопленные находки"): за одну сессию всплыли
# три независимых бага одного и того же класса — запятая-десятичная в
# MicroClimate (climate-поля молча пустели), устаревший EXPECTED_ACTIVE_N8N
# (ложная тревога месяцами), multiline-параметр ломал n8n-ноду записи. Все
# три нашлись случайно, не системной проверкой. Ниже — два системных чека,
# которые ловят СЛЕДУЮЩИЕ такие находки автоматически, а не по счастливой
# случайности во время несвязанной задачи.
_NUTRIENT_NUMERIC_COLS = [
    "Calories", "Proteins", "Carbs", "Fats", "Магний", "Витамин D",
    "Омега-3 (EPA/DHA)", "Селен", "Йод", "Калий", "Железо", "Кальций",
    "Витамин B12", "Витамин К", "Витамин Е", "Цинк", "Клетчатка", "Холестерин",
    "Добавленный сахар", "Натрий", "Кофеин", "Насыщенные жиры", "Трансжиры",
]


def _check_numeric_garbage(cur, problems: list, notes: list) -> None:
    """Ловит именно тот класс бага, что нашёлся живой проверкой в MicroClimate
    (temperature="26,4" — запятая-десятичная, float() падает молча внутри
    try/except, поле навсегда пустое): сканирует нутриент-колонки health.meals
    и health.day_sum за последние 10 дней на значения, которые не парсятся
    как число ни как есть, ни после replace(',', '.'). Не проверяет
    health.daily_trends — эта таблица пишется card-service'ом же самим
    (biohacking_ingest.py уже форматирует числа единообразно через _js_str),
    риск локали там не у нас, а у внешних источников на входе."""
    since = (_vl_now() - timedelta(days=10)).date()
    for table, date_col in (("meals", "Date"), ("day_sum", "Date")):
        cols = ", ".join(f'"{c}"' for c in _NUTRIENT_NUMERIC_COLS)
        try:
            cur.execute(f'SELECT "{date_col}", {cols} FROM health.{table} WHERE "{date_col}" >= %s', (since,))
        except Exception as e:
            notes.append(f"{table}: проверка на мусор в числах не удалась ({str(e)[:60]})")
            continue
        rows = cur.fetchall()
        if not rows:
            continue
        bad = set()
        for row in rows:
            for col, val in zip(_NUTRIENT_NUMERIC_COLS, row[1:]):
                if val is None or str(val).strip() == "":
                    continue
                try:
                    float(str(val).replace(",", "."))
                except ValueError:
                    bad.add(col)
        if bad:
            problems.append(f"{table}: не парсятся как число (проверь формат/локаль): {', '.join(sorted(bad))}")


def _check_anomaly_freshness(cur, problems: list, notes: list) -> None:
    """Anomaly_Detector — на card-service с #34, три независимых пути запуска
    (после ингеста + два расписания), поэтому «тихо перестал работать» не
    выглядело бы как ошибка нигде — просто health.anomaly_log перестал бы
    расти. Сверяем: если у daily_trends есть свежая дата, у anomaly_log
    должна быть запись на неё же (или на день раньше — таймингом расписаний)."""
    cur.execute('SELECT max("Дата") FROM health.daily_trends')
    row = cur.fetchone()
    latest_trend = row[0] if row else None
    if latest_trend is None:
        notes.append("anomaly_log: daily_trends пуст, проверка свежести пропущена")
        return
    cur.execute("SELECT max(date) FROM health.anomaly_log")
    row = cur.fetchone()
    latest_anomaly = row[0] if row else None
    if latest_anomaly is None:
        problems.append("🔴 health.anomaly_log пуст, хотя daily_trends не пуст — детектор аномалий вообще не пишет")
        return
    gap_days = (latest_trend - latest_anomaly).days
    if gap_days > 1:
        problems.append(
            f"health.anomaly_log отстаёт от daily_trends на {gap_days} дн. "
            f"(последняя запись {latest_anomaly}, а Daily_Trends уже {latest_trend}) — детектор аномалий не запускался?"
        )


def _check_load_gate(today_cache: dict, problems: list, notes: list) -> None:
    """Инвариант безопасности: у пациента активная грыжа L5/S1 в Patient_State —
    гейт ОБЯЗАН быть blocked. Если today-dashboard отдаёт blocked=false, это
    регрессия loadGate (fail-open), не мелочь."""
    gate = ((today_cache or {}).get("decision") or {}).get("gate")
    if not gate:
        notes.append("гейт: today-dashboard не отдал decision.gate — проверка пропущена")
        return
    if gate.get("blocked") is not True:
        problems.append(
            f"🔴 ГЕЙТ НАГРУЗКИ ОТКРЫТ (blocked={gate.get('blocked')}, source={gate.get('source', '?')}). "
            "При активной грыже L5/S1 это регрессия loadGate — проверь app/patient_gate.py::load_gate()."
        )
    else:
        verdict = str((today_cache.get("decision") or {}).get("verdict") or "")
        if "ходьб" not in verdict.lower() and "плаван" not in verdict.lower():
            problems.append(f"гейт blocked, но verdict «{verdict}» не про ходьбу/плавание — проверь")


def _check_garmin_ingest_failures(cur, problems: list, notes: list) -> None:
    """Аудит (ZCode, AGENT_SYNC #38): health.garmin_ingest_log (стейджинг сырого
    Garmin-payload'а, премортем #7, app/biohacking_ingest.py) писал строки со
    status='failed' при сбое разбора, но ничто не читало эту колонку — сбой
    разбора мог копиться неделями незамеченным. Смотрим последние 3 дня."""
    since = (_vl_now() - timedelta(days=3)).date().isoformat()
    cur.execute(
        "SELECT count(*), max(received_at) FROM health.garmin_ingest_log "
        "WHERE status = 'failed' AND received_at >= %s", (since,),
    )
    row = cur.fetchone()
    cnt = row[0] if row else 0
    if cnt:
        problems.append(f"health.garmin_ingest_log: {cnt} failed за последние 3 дня (последний {row[1]}) — разбор Garmin-payload'а падает")


def build_message() -> dict:
    problems: list = []
    notes: list = [
        "Recommendations_Log (свежесть советника) отдельно не проверяется — сам "
        "Weekly AI Advisor шлёт явное предупреждение в Telegram при сбое",
    ]

    today_cache = _check_webhooks(problems, notes)
    with get_conn() as conn, conn.cursor() as cur:
        _check_gaps(cur, problems, notes)
        _check_pg_status(cur, problems)
        _check_numeric_garbage(cur, problems, notes)
        _check_anomaly_freshness(cur, problems, notes)
        _check_garmin_ingest_failures(cur, problems, notes)
    _check_load_gate(today_cache, problems, notes)

    ts = _vl_now().strftime("%Y-%m-%d %H:%M")
    if problems:
        msg = f"⚠️ <b>Система: проблемы ({ts} ВЛ)</b>\n\n• " + "\n• ".join(problems)
        if notes:
            msg += "\n\n<i>" + "; ".join(notes) + "</i>"
    elif notes:
        msg = f"✅ Система в норме ({ts} ВЛ), но: " + "; ".join(notes) + "."
    else:
        msg = (
            f"✅ Система в норме ({ts} ВЛ). Дашборд-эндпоинты отвечают без ошибок, "
            "Daily_Trends/day_sum/Meals без дыр за 5 дней, числа в питании парсятся, "
            "anomaly_log свежий, Garmin-ingest без сбоев за 3 дня, гейт blocked (грыжа)."
        )
    return {"message": msg, "has_problems": bool(problems), "problems": problems, "notes": notes}


def run_once() -> dict:
    result = build_message()
    telegram.send_message(CHAT_ID, result["message"], parse_mode="HTML")
    return result


def run_scheduler() -> None:
    """Тот же паттерн, что app.doctor.anamnesis.run_scheduler: спит до 08:43 VL,
    зовёт run_once, повторяет; сбой цикла не убивает поток."""
    logger.info("system_check scheduler: старт")
    while True:
        try:
            now = _vl_now()
            nxt = now.replace(hour=CHECK_HOUR_VL, minute=CHECK_MINUTE_VL, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            run_once()
        except Exception as e:
            logger.exception("system_check run_once упал — повтор завтра")
            alert_on_failure("system_check", e)
            time.sleep(3600)
