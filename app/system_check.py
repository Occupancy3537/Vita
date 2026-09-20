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
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from app.db import get_conn
from app.doctor import telegram

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
CHECK_HOUR_VL = 8
CHECK_MINUTE_VL = 43

_N8N_BASE = "http://n8n:443/webhook/"
_N8N_API = "http://n8n:443/api/v1/"
_N8N_API_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiNGE3MWNhZi03ZjVkLTQ1YjEtODE4MC03MTU4YTZkODA2OTciLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiZGYwOTYwODUtNzM5Zi00NWI1LWFmNWQtZGI5YjIxMzgwM2U0IiwiaWF0IjoxNzg3NjE5Mzg0LCJleHAiOjE4MTkxMTYwMDB9.EB9Nla8jN5J7ZfXITC3okLsLlUQM5o9-uY7ZgTayHwk"
)

# (label, url, max_age_hours) — полный URL, не только path: экраны переезжают на
# card-service по одному (bioage — 2026-09-19, #22), у каждого свой адрес после
# переезда (localhost, а не путь у n8n) — 1:1 с n8n-версией только для тех, что
# ещё не перенесены. (health-dashboard уже убран из проверки, 2026-09-16 —
# экран «Здоровье» из card-service, у него отдельный вечноживой /dashboard/health
# внутри самого card-service, тут отдельно не проверяем.)
WEBHOOK_CHECKS = [
    ("today-dashboard", "http://127.0.0.1:8080/dashboard/today?token=QpcRi1JgTF75uzOf4WrV", 4),
    ("bioage-dashboard", "http://127.0.0.1:8080/dashboard/bioage?token=QpcRi1JgTF75uzOf4WrV", 27),
    ("weekly-nutrients", "http://127.0.0.1:8080/dashboard/weekly-nutrition?token=QpcRi1JgTF75uzOf4WrV", 30),
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

# Критичные воркфлоу — обновлено под текущую архитектуру (2026-09-20, #24/#25/#33):
# убраны сознательно неактивные (Capitan/relay, старые Sub-Agent'ы, Anamnesis
# Collector, health-dashboard cache, bioage-dashboard cache — на card-service
# с #22, today-dashboard cache — на card-service с #24, Dashboard Cached
# (today-nutrition) — на card-service с #25, Diet Quality Tagger — на card-service
# с #30, Health Watchdog — на card-service с #31, Reports — на card-service с
# #32, Weekly AI Advisor — на card-service с #33, закрывает группу 2 целиком) —
# держать их в списке значило бы получать ложную тревогу каждое утро за то,
# что уже и так правильно выключено.
# НАХОДКА (2026-09-20, #33): "_System Check" сам оставался в этом списке с
# момента своего же переноса в #21 — сам себя не вычеркнул при отключении в
# n8n, то есть эта проверка ежедневно молча слала бы "🔴 НЕ АКТИВНЫ: _System
# Check" с 19.09 (не проверял историю отправленных сообщений — увидел только
# сейчас, сверяя список активных воркфлоу перед отключением Advisor). Убрано.
EXPECTED_ACTIVE_N8N = [
    "_Error Handler",
    "PhenoAge Calc", "Anomaly_Detector/Correlations",
]
# _Backup Alert — на card-service с 2026-09-20 (app/backup_alert.py, группа малых утилит).


def _d10(v) -> str:
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v or "")[:10]


def _vl_now() -> datetime:
    return datetime.now(VL)


def _check_webhooks(problems: list, notes: list) -> dict:
    """Возвращает ответ today-dashboard (нужен ниже для проверки гейта) —
    та же экономия одного лишнего запроса, что была в оригинале."""
    today_cache = None
    for path, url, max_h in WEBHOOK_CHECKS:
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
            continue
        ua = res.get("updated_at") or res.get("computed_at") if isinstance(res, dict) else None
        if ua and isinstance(ua, str) and len(ua) >= 11 and ua[10] == "T":
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(ua.replace("Z", "+00:00"))).total_seconds() / 3600
                if age_h > max_h:
                    problems.append(f"{path}: кэш устарел на {round(age_h)}ч (порог {max_h}ч)")
            except ValueError:
                problems.append(f"{path}: не смог разобрать метку времени {ua!r}")
        else:
            problems.append(f"{path}: ответ без метки времени (updated_at/computed_at)")
    return today_cache or {}


def _check_dashboard_widget(problems: list) -> None:
    try:
        r = httpx.get(_N8N_BASE + "dashboard?token=wFSIRB6DO4l6ZrUSlJR5", timeout=20.0)
        r.raise_for_status()
        if "<!doctype" not in r.text.lower() and "<html" not in r.text.lower():
            problems.append("dashboard (виджет): не отдаёт HTML")
    except Exception as e:
        problems.append(f"dashboard (виджет): не отвечает ({str(e)[:60]})")


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
            "При активной грыже L5/S1 это регрессия loadGate — проверь today_build.js / bc."
        )
    else:
        verdict = str((today_cache.get("decision") or {}).get("verdict") or "")
        if "ходьб" not in verdict.lower() and "плаван" not in verdict.lower():
            problems.append(f"гейт blocked, но verdict «{verdict}» не про ходьбу/плавание — проверь")


def _check_n8n_active(problems: list, notes: list) -> None:
    try:
        r = httpx.get(_N8N_API + "workflows?limit=250", headers={"X-N8N-API-KEY": _N8N_API_KEY}, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        active_names = {w["name"] for w in data.get("data", []) if w.get("active")}
        missing = [n for n in EXPECTED_ACTIVE_N8N if n not in active_names]
        if missing:
            problems.append("🔴 НЕ АКТИВНЫ критичные воркфлоу: " + ", ".join(missing))
    except Exception as e:
        notes.append(f"проверка active-воркфлоу не удалась ({str(e)[:50]}) — n8n API?")


def build_message() -> dict:
    problems: list = []
    notes: list = [
        "Recommendations_Log (свежесть советника) отдельно не проверяется — сам "
        "Weekly AI Advisor шлёт явное предупреждение в Telegram при сбое",
    ]

    today_cache = _check_webhooks(problems, notes)
    _check_dashboard_widget(problems)
    with get_conn() as conn, conn.cursor() as cur:
        _check_gaps(cur, problems, notes)
        _check_pg_status(cur, problems)
    _check_load_gate(today_cache, problems, notes)
    _check_n8n_active(problems, notes)

    ts = _vl_now().strftime("%Y-%m-%d %H:%M")
    if problems:
        msg = f"⚠️ <b>Система: проблемы ({ts} ВЛ)</b>\n\n• " + "\n• ".join(problems)
        if notes:
            msg += "\n\n<i>" + "; ".join(notes) + "</i>"
    elif notes:
        msg = f"✅ Система в норме ({ts} ВЛ), но: " + "; ".join(notes) + "."
    else:
        msg = (
            f"✅ Система в норме ({ts} ВЛ). Вебхуки живы, кэши свежие, Daily_Trends/day_sum/Meals "
            "без дыр за 5 дней, гейт blocked (грыжа), критичные воркфлоу active."
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
        except Exception:
            logger.exception("system_check run_once упал — повтор завтра")
            time.sleep(3600)
