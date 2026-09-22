"""Сборка JSON для страницы «Настройки» (/dashboard/system-status, 2026-09-22).

Форма ответа — та, что читает статическая страница settings.html («один взгляд:
деньги/память/процессор + вердикт, остальное — под каты»). Подробности выбора
полей и реестров — в TIME_AND_MULTIUSER_PLAN и AGENT_SYNC #60.

Источники: /proc (хостовые метрики — внутри контейнера значения хостовые),
card.host_metrics (пик load за сутки), card.agent_step (стоимость/латентность
LLM — пока только путь доктора, остальные модули стоимость не пишут),
card.scheduler_run_log (прогоны циклов, app/run_log.py), таблицы-источники
(«свежесть данных»), health.backup_alert_state + card.err_dedup_state (ночные
процессы), env/модули (модели и ФАКТ наличия секретов — значения не отдаём
никогда), health.patient_state (гейт нагрузки, через dashboard).

Fail-safe: каждая секция независима — при сбое отдаётся degraded-заглушка
(None/пустой список), страница наблюдения не должна падать целиком из-за одной
таблицы (тот же принцип, что у остальных read-путей).
"""
import logging
import os
from datetime import date, datetime, timezone
from typing import Callable, Optional

from app import ai_models, host_metrics, registrar, timeutil
from app.db import schema
from app.doctor import config as doctor_config

logger = logging.getLogger(__name__)

# Бюджет OpenRouter на день, поднят Владом 2026-09-16 (см. doctor/config.py,
# история перехода на GLM 5.3) — для отображения «$X из $Y».
DAILY_BUDGET_USD = 1.50

# Имена обязательных секретов — синхронизированы с проверками run.sh
# (": ${VAR:?...}"). Тест tests/test_system_status.py сверяет этот список с
# самим run.sh: при добавлении секрета туда тест упадёт и напомнит обновить тут.
SECRET_NAMES = [
    "CARD_PG_PASSWORD", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN",
    "FOOD_DIARY_BOT_TOKEN", "YANDEX_IOT_TOKEN", "CARD_GOOGLE_CLIENT_ID",
    "CARD_GOOGLE_CLIENT_SECRET", "CARD_GOOGLE_SHEETS_REFRESH_TOKEN",
    "CARD_GOOGLE_CALENDAR_REFRESH_TOKEN", "RESCUETIME_API_KEY",
    "DASHBOARD_TOKEN", "WIDGET_TOKEN", "ERR_DEDUP_TOKEN", "HERMES_BOT_TOKEN",
    "NUTRITION_BOT_TOKEN", "ACTION_ACK_TOKEN", "BACKUP_STATUS_TOKEN",
]

# Реестр фоновых циклов: ключ = имя в card.scheduler_run_log (совпадает с именем
# в alert_on_failure), человеческое имя/расписание/флаг — из main._STARTUP_TASKS
# (проверка соответствия — tests/test_system_status.py).
LOOPS: list[dict] = [
    {"key": "doctor_poller", "n": "Приём сообщений (доктор)", "s": "круглосуточно",
     "flag": "TELEGRAM_POLLING_ENABLED"},
    {"key": "food_diary_bot_poller", "n": "Приём сообщений (дневник еды)", "s": "круглосуточно",
     "flag": "FOOD_DIARY_BOT_ENABLED"},
    {"key": "card_processor", "n": "Разбор входящих записей", "s": "каждые 5 мин",
     "flag": "CARD_PROCESSOR_ENABLED"},
    {"key": "host_metrics", "n": "Метрики машины", "s": "каждые 5 мин",
     "flag": "HOST_METRICS_ENABLED"},
    {"key": "gate_watch", "n": "Сторож медограничения", "s": "каждые 15 мин",
     "flag": "GATE_WATCH_ENABLED"},
    {"key": "yandex_climate", "n": "Климат в спальне", "s": "каждый час",
     "flag": "YANDEX_CLIMATE_ENABLED"},
    {"key": "system_check", "n": "Общая проверка системы", "s": "ежедневно 08:43",
     "flag": "SYSTEM_CHECK_ENABLED"},
    {"key": "memory_archive_check", "n": "Проверка памяти", "s": "ежедневно 08:00",
     "flag": "SMALL_ALERTS_ENABLED"},
    {"key": "backup_alert", "n": "Проверка бэкапа", "s": "ежедневно 09:00 UTC",
     "flag": "SMALL_ALERTS_ENABLED"},
    {"key": "health_watchdog", "n": "Сторож здоровья", "s": "ежедневно 09:00",
     "flag": "HEALTH_WATCHDOG_ENABLED"},
    {"key": "meds_from_calendar", "n": "Лекарства из календаря", "s": "ежедневно 09:00",
     "flag": "MEDS_FROM_CALENDAR_ENABLED"},
    {"key": "anomaly_detector_daily", "n": "Поиск аномалий (день)", "s": "ежедневно 09:15",
     "flag": "ANOMALY_DETECTOR_ENABLED"},
    {"key": "anamnesis", "n": "Сбор анамнеза", "s": "ежедневно 11:00",
     "flag": "ANAMNESIS_SCHEDULER_ENABLED"},
    {"key": "nutrition_reports_daily", "n": "Отчёт о питании (день)", "s": "ежедневно 21:45",
     "flag": "NUTRITION_REPORTS_ENABLED"},
    {"key": "phenoage_calc", "n": "Расчёт биовозраста", "s": "по воскресеньям 09:00",
     "flag": "PHENOAGE_CALC_ENABLED"},
    {"key": "anomaly_detector_weekly", "n": "Поиск аномалий (неделя)", "s": "по воскресеньям 11:00",
     "flag": "ANOMALY_DETECTOR_ENABLED"},
    {"key": "nutrition_reports_weekly", "n": "Отчёт о питании (неделя)", "s": "по воскресеньям 12:00",
     "flag": "NUTRITION_REPORTS_ENABLED"},
    {"key": "weekly_advisor", "n": "Недельный советник", "s": "по воскресеньям 20:00",
     "flag": "WEEKLY_ADVISOR_ENABLED"},
    {"key": "monthly_trend", "n": "Месячный тренд", "s": "1-го числа 10:00",
     "flag": "MONTHLY_TREND_ENABLED"},
]

# «Свежесть данных»: источник → запрос последней метки времени. ok_h — порог
# «свежо»; None = информационная строка без порога (анализы/анкета — по факту).
FRESHNESS: list[dict] = [
    {"n": "Garmin (сон, пульс, шаги)", "s": "ночью, автоматически",
     "sql": 'SELECT max("Дата") FROM health.daily_trends', "ok_h": 48},
    {"n": "Дневник еды", "s": "после каждого приёма пищи",
     "sql": 'SELECT max("Date") FROM health.meals', "ok_h": 48},
    {"n": "Климат в спальне", "s": "раз в час",
     "sql": 'SELECT max("Дата") FROM health.microclimate', "ok_h": 6},
    {"n": "Итоги питания за день", "s": "вечерним отчётом",
     "sql": 'SELECT max("Date") FROM health.day_sum', "ok_h": 48},
    {"n": "Анализы из лабораторий", "s": "по факту сдачи",
     "sql": "SELECT max(ts_event) FROM {card}.lab_result", "ok_h": None},
    {"n": "Советы советника", "s": "еженедельно",
     "sql": 'SELECT max("Date") FROM health.recommendations_log', "ok_h": 24 * 8},
    {"n": "Анкета анамнеза", "s": "ежедневно 11:00",
     "sql": 'SELECT max(coalesce("Answered_Date", "Asked_Date")) FROM health.anamnesis', "ok_h": None},
    {"n": "Факты с часов", "s": "ночью",
     "sql": "SELECT max(ts_event) FROM {card}.fact", "ok_h": 48},
]

# «Ночные процессы»: n — имя, d — пояснение под катом, err_key — ключ в
# card.err_dedup_state (кроме бэкапа: у него пинг в health.backup_alert_state).
NIGHTLY: list[dict] = [
    {"n": "Ночной бэкап → 2 облака", "err_key": None,
     "d": "pg_dump + конфиги + проект → rclone crypt в Google Drive и OneDrive. Пинг приходит в card-service; алерты — при сбое и при отсутствии пинга >26 ч."},
    {"n": "Перенос в Google Sheets", "err_key": "pg_to_sheets_mirror",
     "d": "PG → Sheets (Symptom_Log / Doctor_Notes / Investigations). Положительный результат никуда не пишется — видно только ошибки."},
    {"n": "Перенос из Google Sheets", "err_key": "sheets_to_pg_mirror",
     "d": "Sheets → PG (витамины, препараты, пациент, микроклимат, профиль). Виден только факт отсутствия ошибок."},
    {"n": "Сверка базы и таблиц", "err_key": "pg_sheets_diff_check",
     "d": "Сверяет meals/daily_trends; при расхождении сверх порога — алерт в Telegram."},
]

MIRROR_QUIET_HOURS = 72  # «ошибок не было» смотрим за 3 суток (ночные скрипты раз в сутки)


def _sec(label: str, fn: Callable, default):
    """Секция собирается независимо: сбой одной не роняет страницу."""
    try:
        return fn()
    except Exception:
        logger.exception("system_status: секция %s упала — отдаю degraded", label)
        return default


# --- хост (внутри контейнера /proc показывает значения хоста) ---------------

def host_block() -> Optional[dict]:
    try:
        load1, load5, load15 = (float(x) for x in open("/proc/loadavg").read().split()[:3])
        mem: dict[str, int] = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])
        uptime_days = float(open("/proc/uptime").read().split()[0]) / 86400
    except Exception:
        logger.exception("system_status: не удалось прочитать /proc")
        return None
    cores = os.cpu_count() or 1
    mem_total_gb = mem["MemTotal"] / 1024 / 1024
    mem_used_gb = (mem["MemTotal"] - mem["MemAvailable"]) / 1024 / 1024
    swap_total_gb = mem["SwapTotal"] / 1024 / 1024
    swap_used_gb = (mem["SwapTotal"] - mem["SwapFree"]) / 1024 / 1024
    return {
        "cores": cores,
        "load1": load1, "load5": load5, "load15": load15,
        "now_pct": round(load1 / cores * 100),
        "ram_used_gb": round(mem_used_gb, 1), "ram_total_gb": round(mem_total_gb, 1),
        "swap_used_gb": round(swap_used_gb, 1), "swap_total_gb": round(swap_total_gb, 1),
        "uptime_days": round(uptime_days),
    }


# --- прочие секции ----------------------------------------------------------

def _money(cur) -> dict:
    t = schema() + ".agent_step"
    tz_name = str(timeutil.person_tz())
    cur.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM {t} "
        "WHERE role = 'model' AND ts >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s".format(t=t),
        (tz_name, tz_name),
    )
    today = float(cur.fetchone()[0] or 0)
    cur.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM {t} "
        "WHERE role = 'model' AND ts >= now() - interval '7 days'".format(t=t),
    )
    week = float(cur.fetchone()[0] or 0)
    cur.execute(
        "SELECT count(DISTINCT turn_id), coalesce(sum(cost_usd), 0), avg(latency_ms) "
        "FROM {t} WHERE role = 'model' AND ts >= now() - interval '7 days'".format(t=t),
    )
    turns, week_cost, avg_ms = cur.fetchone()
    note = "ходов доктора за неделю не было"
    if turns:
        note = "$%.2f за ход доктора · ответ ~%d с" % (float(week_cost or 0) / int(turns),
                                                       round(float(avg_ms or 0) / 1000))
    return {
        "providers": [{
            "n": "OpenRouter",
            "purpose": "доктор, чтение анализов, недельные советы",
            "today": round(today, 2),
            "cap": DAILY_BUDGET_USD,
            "week": round(week, 2),
            "note": note,
        }],
        "future": ["Anthropic", "Google Gemini", "OpenAI"],
        "uncovered": "учтены вызовы доктора; советник, регистратор и дневник еды стоимость пока не записывают",
    }


def _loops(cur) -> list[dict]:
    cur.execute("SELECT name, last_ok_at, last_error, last_error_at FROM {t}".format(
        t=schema() + ".scheduler_run_log"))
    rows = {r[0]: r for r in cur.fetchall()}
    now = datetime.now(timezone.utc)
    out = []
    for spec in LOOPS:
        r = rows.get(spec["key"])
        last_ok_h = None
        if r and r[1] is not None:
            last_ok_h = round((now - r[1]).total_seconds() / 3600, 1)
        out.append({
            "key": spec["key"], "n": spec["n"], "s": spec["s"], "flag": spec["flag"],
            "last_ok_h": last_ok_h,
            "last_error": (r[2] if r else None),
            "last_error_h": (round((now - r[3]).total_seconds() / 3600, 1) if r and r[3] else None),
        })
    return out


def _fmt_moment(raw) -> tuple[Optional[str], Optional[float]]:
    """(подпись для страницы, возраст в часах). Принимает timestamptz/date/строку."""
    if raw is None:
        return None, None
    tz = timeutil.person_tz()
    now = datetime.now(timezone.utc)
    dt: Optional[datetime] = None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    elif isinstance(raw, date):
        dt = datetime(raw.year, raw.month, raw.day, tzinfo=tz)
    else:
        s = str(raw).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:len(fmt) + 2].strip(), fmt).replace(tzinfo=tz)
                break
            except ValueError:
                continue
    if dt is None:
        return str(raw)[:16], None
    age_h = max(0.0, (now - dt).total_seconds() / 3600)
    local = dt.astimezone(tz)
    today = timeutil.today()
    if local.date() == today:
        return "сегодня " + local.strftime("%H:%M"), round(age_h, 1)
    if (today - local.date()).days == 1:
        return "вчера " + local.strftime("%H:%M"), round(age_h, 1)
    return local.strftime("%d.%m"), round(age_h, 1)


def _freshness(cur) -> list[dict]:
    out = []
    for spec in FRESHNESS:
        cur.execute(spec["sql"].format(card=schema()))
        raw = cur.fetchone()[0]
        value, age_h = _fmt_moment(raw)
        ok = True
        if spec["ok_h"] is not None and age_h is not None:
            ok = age_h <= spec["ok_h"]
        out.append({"n": spec["n"], "s": spec["s"], "v": value or "нет данных", "ok": ok})
    return out


def _nightly(cur) -> list[dict]:
    # Бэкап — из состояния пинга; зеркала — из err_dedup (видно только ошибки)
    cur.execute("SELECT result, detail, stamp, extract(epoch from now() - ts) / 3600 FROM health.backup_alert_state WHERE id = 1")
    backup = cur.fetchone()
    cur.execute("SELECT key, extract(epoch from now() - last_notified_at) / 3600 FROM {t}".format(
        t=schema() + ".err_dedup_state"))
    dedup = {r[0]: float(r[1]) for r in cur.fetchall()}

    out = []
    for spec in NIGHTLY:
        st, v, d = "ok", "", spec["d"]
        if spec["err_key"] is None:  # бэкап
            if not backup:
                st, v = "warn", "пинг ещё не приходил"
            else:
                result, detail, stamp, age_h = backup
                if result == "ok":
                    st, v = "ok", "ок · %s" % (stamp or "?")
                elif result == "partial":
                    st, v = "warn", "частично · %s" % (stamp or "?")
                else:
                    st, v = "bad", "сбой: %s · %s" % (detail or "?", stamp or "?")
                if age_h is not None and age_h > 26 and st == "ok":
                    st, v = "warn", "пинга не было %d ч (последний %s)" % (round(age_h), stamp or "?")
        else:
            err_h = dedup.get(spec["err_key"])
            if err_h is not None and err_h <= MIRROR_QUIET_HOURS:
                st = "warn"
                v = "ошибка %s ч назад" % round(err_h) if err_h >= 1 else "ошибка только что"
            else:
                v = "ошибок за 3 суток нет"
        out.append({"n": spec["n"], "st": st, "v": v, "d": d})
    return out


def _config(cur) -> dict:
    models = [
        {"n": "Доктор", "v": "%s · effort %s" % (doctor_config.DOCTOR_MODEL,
                                                 doctor_config.DOCTOR_REASONING_EFFORT)},
        {"n": "Разбор жалоб / извлечение", "v": ai_models.DEFAULT_MODEL},
        {"n": "Дневник еды (фото и текст)", "v": ai_models.FOOD_MODEL},
        {"n": "Регистратор: фото анализов", "v": registrar.REGISTRAR_MODEL},
        {"n": "Регистратор: PDF анализов", "v": registrar.REGISTRAR_PDF_MODEL},
    ]
    present = [name for name in SECRET_NAMES if os.environ.get(name)]
    gate = None
    try:
        from app.dashboard import get_today_dashboard
        today = get_today_dashboard(cur)
        g = ((today.get("decision") or {}).get("gate")) or None
        if g:
            gate = {
                "blocked": bool(g.get("blocked")),
                "condition": g.get("condition"),
                "allowed": g.get("allowed") or "",
                "contra": g.get("contra") or "",
                "source": g.get("source"),
                "review_due": str(g.get("review_due") or ""),
            }
    except Exception:
        logger.exception("system_status: не удалось собрать гейт нагрузки")
    return {"models": models, "secrets_ok": len(present), "secrets_total": len(SECRET_NAMES),
            "gate": gate}


def build(cur) -> dict:
    """Собрать ответ страницы. cur — курсор активного соединения."""
    money = _sec("money", lambda: _money(cur), None)
    host = _sec("host", host_block, None)
    peak = _sec("peak", lambda: host_metrics.peak_since(cur), None)
    loops = _sec("loops", lambda: _loops(cur), [])
    freshness = _sec("freshness", lambda: _freshness(cur), [])
    nightly = _sec("nightly", lambda: _nightly(cur), [])
    config = _sec("config", lambda: _config(cur), {"models": [], "secrets_ok": 0,
                                                   "secrets_total": len(SECRET_NAMES), "gate": None})
    local_now = timeutil.now_local()
    return {
        "ts": local_now.strftime("%d.%m %H:%M") + " VL",
        "money": money,
        "host": host,
        "peak24": peak,
        "loops": loops,
        "freshness": freshness,
        "nightly": nightly,
        "config": config,
    }
