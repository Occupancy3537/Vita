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
никогда), health.patient_state (гейт нагрузки, через dashboard), card.issue_log
(2026-09-23, Шаг 2 «петли самоулучшения» — открытые находки, см. app/issue_log.py;
не то же самое, что «Автоматические проверки» ниже: там — жив ли цикл СЕЙЧАС,
здесь — durable-бэклог того, что уже случалось и не закрыто).

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
    "DASHBOARD_TOKEN", "WIDGET_TOKEN", "ERR_DEDUP_TOKEN",
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
    {"key": "anamnesis", "n": "Анамнез — вопрос дня", "s": "ежедневно 11:00",
     "flag": "ANAMNESIS_SCHEDULER_ENABLED"},
    {"key": "digest", "n": "Вечерний дайджест (сервисный бот)", "s": "ежедневно 21:50",
     "flag": "DIGEST_SCHEDULER_ENABLED"},
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
    {"key": "issue_review", "n": "Разбор бэклога находок", "s": "по воскресеньям 19:00",
     "flag": "ISSUE_REVIEW_ENABLED"},
    {"key": "research_scan", "n": "Научный контур — скан публикаций", "s": "по воскресеньям 21:50",
     "flag": "RESEARCH_SCAN_ENABLED"},
    {"key": "consilium_monthly", "n": "Консилиум специалистов — месячный прогон", "s": "1-е число месяца ~09:30",
     "flag": "CONSILIUM_SCHEDULER_ENABLED"},
    {"key": "problem_maintenance", "n": "Детектив — presumed_resolved по тишине", "s": "ежедневно 09:10",
     "flag": "PROBLEM_MAINTENANCE_ENABLED"},
]

# «Свежесть данных»: источник → запрос последней метки времени. ok_h — порог
# «свежо»; None = информационная строка без порога (анализы/анкета — по факту).
FRESHNESS: list[dict] = [
    {"n": "Garmin (сон, пульс, шаги)", "s": "ночью, автоматически",
     "sql": 'SELECT max("Дата") FROM health.daily_trends', "ok_h": 48},
    {"n": "Дневник еды", "s": "после каждого приёма пищи",
     "sql": 'SELECT max("Date") FROM health.meals', "ok_h": 48},
    {"n": "Климат в спальне", "s": "сенсор раз в час → в базу ночным зеркалом",
     "sql": 'SELECT max("Дата") FROM health.microclimate', "ok_h": 26},
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

    def _sum(sql_tpl: str, params: tuple) -> float:
        """Сумма стоимости двух источников: доктор (agent_step, структурный
        трейс) + остальные модули (llm_usage; с 2026-09-22 пишут советник,
        регистратор, дневник еды, извлечение, red-flag B, диспетчер, память L2,
        отчёты, watchdog — «полные расходы» по запросу Влада)."""
        total = 0.0
        cur.execute(sql_tpl.format(t=t), params)
        total += float(cur.fetchone()[0] or 0)
        cur.execute(sql_tpl.format(t=schema() + ".llm_usage"), params)
        total += float(cur.fetchone()[0] or 0)
        return total

    day_sql = ("SELECT coalesce(sum(cost_usd), 0) FROM {t} "
               "WHERE ts >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s")
    today = _sum(day_sql, (tz_name, tz_name))
    today = _round_money(today)

    week_sql = "SELECT coalesce(sum(cost_usd), 0) FROM {t} WHERE ts >= now() - interval '7 days'"
    week = _round_money(_sum(week_sql, ()))

    cur.execute(
        "SELECT count(DISTINCT turn_id), coalesce(sum(cost_usd), 0), avg(latency_ms) "
        "FROM {t} WHERE role = 'model' AND ts >= now() - interval '7 days'".format(t=t),
    )
    turns, _week_cost, avg_ms = cur.fetchone()
    note = "ходов доктора за неделю не было"
    if turns:
        note = "$%.2f за ход доктора · ответ ~%d с" % (float(_week_cost or 0) / int(turns),
                                                       round(float(avg_ms or 0) / 1000))
    return {
        "providers": [{
            "n": "OpenRouter",
            "purpose": "доктор, чтение анализов, недельные советы",
            "today": today,
            "cap": DAILY_BUDGET_USD,
            "week": week,
            "note": note,
        }],
        "future": ["Anthropic", "Google Gemini", "OpenAI"],
        "uncovered": "учтены все вызовы: доктор + советник, регистратор, дневник еды, "
                     "извлечение, классификатор, память, отчёты, watchdog",
    }


def _round_money(v: float) -> float:
    return round(v + 1e-9, 2)  # +1e-9 — чтобы 0.005 не «прыгал» вниз из-за float


def _loops(cur) -> list[dict]:
    cur.execute("SELECT name, last_ok_at, last_error, last_error_at FROM {t}".format(
        t=schema() + ".scheduler_run_log"))
    rows = {r[0]: r for r in cur.fetchall()}
    now = datetime.now(timezone.utc)
    out = []
    for spec in LOOPS:
        r = rows.get(spec["key"])
        last_ok_at = r[1] if r else None
        last_error_at = r[3] if r else None
        last_ok_h = round((now - last_ok_at).total_seconds() / 3600, 1) if last_ok_at is not None else None
        # 2026-09-23 (реальная находка Влада — ложные предупреждения на
        # "Настройках"): run_log.mark_error() нарочно НЕ стирает last_error
        # успешным прогоном (история "ошибка была, потом ок" — см. run_log.py),
        # но без этого флага страница вечно подсвечивала уже пережитый сбой
        # как ТЕКУЩУЮ проблему часами/сутками после того, как цикл
        # восстановился. current — true, только если ошибка новее последнего
        # успеха (или успеха не было вообще) — то есть цикл ДЕЙСТВИТЕЛЬНО ещё
        # не оправился, а не просто "когда-то падал".
        error_is_current = last_error_at is not None and (last_ok_at is None or last_error_at > last_ok_at)
        out.append({
            "key": spec["key"], "n": spec["n"], "s": spec["s"], "flag": spec["flag"],
            "last_ok_h": last_ok_h,
            "last_error": (r[2] if r else None),
            "last_error_h": (round((now - last_error_at).total_seconds() / 3600, 1) if last_error_at else None),
            "error_is_current": error_is_current,
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
        # 2026-09-23 (реальная находка Влада — "Климат в спальне" ложно горел
        # "вчера 00:00"/stale): health.microclimate."Дата" — text-колонка,
        # yandex_climate.py пишет datetime.now(UTC).isoformat(), т.е.
        # "2026-09-22T02:05:08.107999+00:00" — формат с "T", который старый
        # цикл strptime() ниже ни разу не пробовал (только форматы с пробелом
        # между датой и временем), поэтому ВСЕГДА проваливался до последнего
        # "%Y-%m-%d" — терял час/минуты И трактовал полученную полночь как
        # ЛОКАЛЬНУЮ (Владивосток, UTC+10), а не UTC. На реальных данных это
        # раздувало возраст на ~12-22 часа — свежие (22ч) данные показывались
        # старше порога (26ч) и горели предупреждением без всякой причины.
        # fromisoformat() (3.11+) понимает и "T", и пробел, и одну дату —
        # пробуем его первым, старый цикл — фолбэк на случай других форматов.
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
        except ValueError:
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


_SEVERITY_RANK = {"critical": 0, "important": 1, "minor": 2}


def _issues(cur) -> list[dict]:
    """card.issue_log, только status='open' — snoozed/wontfix/fixed сознательно
    не показываем здесь (это уже принятые решения, не то, что «требует
    внимания сейчас»); полную историю смотреть в самой таблице, не на этой
    странице.

    Возраст считаем В PYTHON от сырых timestamptz (тот же приём, что в
    _loops()), а не extract(epoch...)/3600 в SQL — та форма возвращает
    Decimal, а FastAPI кодирует Decimal в JSON СТРОКОЙ ("0.0" вместо 0.0),
    страница получила бы нечисловое поле (живая проверка 2026-09-23 поймала
    это до деплоя на прод)."""
    cur.execute(
        "SELECT source, severity, summary, occurrences, first_seen, last_seen "
        "FROM {t} WHERE status = 'open' ORDER BY "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'important' THEN 1 ELSE 2 END, last_seen DESC"
        .format(t=schema() + ".issue_log")
    )
    now = datetime.now(timezone.utc)
    return [
        {"source": source, "severity": severity, "summary": summary, "occurrences": occurrences,
         "first_seen_h": round((now - first_seen).total_seconds() / 3600, 1),
         "last_seen_h": round((now - last_seen).total_seconds() / 3600, 1)}
        for source, severity, summary, occurrences, first_seen, last_seen in cur.fetchall()
    ]


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


def timezone_block() -> dict:
    """Часовой пояс человека (Фаза 3): текущая/домашняя зона, «не дома», местное
    время. Используется и страницей «Настройки», и инъекцией в /dashboard/today
    («сегодня · Bangkok»)."""
    from app import people
    p = people.get_person() or {}
    home = p.get("home_tz") or timeutil.DEFAULT_TZ
    cur = str(timeutil.person_tz())
    return {
        "home_tz": home,
        "current_tz": cur,
        "is_travelling": cur != home,
        "local_time": timeutil.now_local().strftime("%H:%M"),
        "examples": people.TZ_EXAMPLES,
    }


def build(cur) -> dict:
    """Собрать ответ страницы. cur — курсор активного соединения."""
    money = _sec("money", lambda: _money(cur), None)
    host = _sec("host", host_block, None)
    peak = _sec("peak", lambda: host_metrics.peak_since(cur), None)
    loops = _sec("loops", lambda: _loops(cur), [])
    freshness = _sec("freshness", lambda: _freshness(cur), [])
    nightly = _sec("nightly", lambda: _nightly(cur), [])
    issues = _sec("issues", lambda: _issues(cur), [])
    config = _sec("config", lambda: _config(cur), {"models": [], "secrets_ok": 0,
                                                   "secrets_total": len(SECRET_NAMES), "gate": None})
    timezone = _sec("timezone", timezone_block, None)
    local_now = timeutil.now_local()
    # T3 (2026-09-23): время уже местное (зона человека) — подпись тоже должна
    # быть про его зону, а не про зашитый «VL»: в поездке «07:45 VL» врало бы.
    tz_label = timeutil.person_tz_name().split("/")[-1].replace("_", " ")
    return {
        "ts": local_now.strftime("%d.%m %H:%M") + " " + tz_label,
        "money": money,
        "host": host,
        "peak24": peak,
        "loops": loops,
        "freshness": freshness,
        "nightly": nightly,
        "issues": issues,
        "config": config,
        "timezone": timezone,
    }
