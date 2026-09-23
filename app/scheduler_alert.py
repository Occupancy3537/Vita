"""Общий алерт на падение фонового цикла (планировщика/поллера).

Независимый аудит (ZCode, AGENT_SYNC #38, находка "[ВЫСОКИЙ] правило CLAUDE.md
«каждый воркфлоу — с errorWorkflow» не перенесено"): у всех n8n-воркфлоу был
errorWorkflow (_Error Handler → Telegram + Error_Log на любой сбой). При
переносе на card-service каждый планировщик получил свой собственный
`except Exception: logger.exception(...); time.sleep(...)` — сбой уходит
только в docker logs, которые никто не читает проактивно. Weekly_advisor
может упасть в воскресенье и никто не узнает до следующего воскресенья.

Фикс — не индивидуальный errorWorkflow на каждый цикл (это n8n-паттерн, для
одного процесса избыточен), а один общий вызов в existing `/err-dedup`
инфраструктуру (app/err_dedup.py, тот же канал/дедуп, что уже используют три
ночных cron-скрипта) — вызывается ВНУТРИ процесса напрямую (без HTTP-петли
на самого себя), т.к. err_dedup теперь просто библиотечная функция card-service.

Использование — один вызов в каждом except-блоке планировщика/поллера:
    except Exception as e:
        logger.exception("X упал — повтор через %ss", INTERVAL_SECONDS)
        alert_on_failure("X", e)
"""
import logging
from datetime import datetime, timezone

from app import run_log
from app.db import get_conn
from app.err_dedup import EXPECTED_TOKEN, run_notify

logger = logging.getLogger(__name__)

MIN_SUSTAINED_SECONDS = 300  # 5 минут — см. alert_on_sustained_failure


def alert_on_failure(source: str, exc: BaseException) -> None:
    """Шлёт (через дедуп 60 мин, как и всё остальное в системе) алерт о
    падении фонового цикла `source`. Сама никогда не бросает исключение —
    вызывается из except-блока, вторичный сбой здесь не должен маскировать
    исходный ни ронять сам планировщик (он должен дожить до следующего
    time.sleep/повтора).

    2026-09-22 (страница «Настройки»): дополнительно пишет ошибку в
    card.scheduler_run_log (app/run_log.py) — алерт живёт 60-минутным дедупом
    и уходит в Telegram, а журнал хранит последнюю ошибку до следующего сбоя
    и читается страницей /dashboard/system-status."""
    run_log.mark_error(source, exc)
    text = f"🔴 {source} упал: {exc}"[:800]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            run_notify(cur, source, "scheduler", text, False, EXPECTED_TOKEN)
            conn.commit()
    except Exception:
        logger.exception("scheduler_alert: не удалось отправить алерт про %s", source)


def alert_on_sustained_failure(source: str, exc: BaseException, failing_since: datetime,
                                min_duration_seconds: float = MIN_SUSTAINED_SECONDS) -> None:
    """Как alert_on_failure(), но только когда сбои идут БЕЗ ПЕРЕРЫВА минимум
    `min_duration_seconds` (по умолчанию 5 мин) — единичные и даже
    многоминутные сетевые обрывы long-polling НИЧЕГО не ломают: retry-цикл
    сам их переживает, ничего не потеряно.

    2026-09-23 (по прямому запросу Влада, версия 2 — предыдущая версия
    считала ПОДРЯД ИДУЩИЕ ПОПЫТКИ, не время; живой инцидент в тот же день
    показал, чем это плохо: getUpdates ловил ReadTimeout/502/Connection
    reset НЕПРЕРЫВНО ~4 минуты подряд и само восстановилось — но версия на
    попытках алертила уже на 3-й (около 15-20с, если сбои быстрые), задолго
    до того, как стало ясно, что это не мгновенный блип. Число попыток —
    ненадёжная мера времени: одна попытка может занять от долей секунды
    (отказ в соединении) до почти полного httpx-таймаута (30с+), так что
    "3 подряд" означает где угодно от 15с до полутора минут в зависимости
    от того, КАК именно рвётся соединение. Реальное время — надёжная мера
    сама по себе.

    failing_since — момент ПЕРВОГО сбоя в текущей НЕПРЕРЫВНОЙ серии (не
    сбрасывается между повторными попытками, вызывающий обязан сбросить его
    в None при первом же успехе — см. app/doctor/poller.py/
    app/food_diary_bot.py)."""
    elapsed = (datetime.now(timezone.utc) - failing_since).total_seconds()
    if elapsed < min_duration_seconds:
        logger.info("%s: сбой идёт %.0fс (< %.0fс) — транзиентно, не алерчу", source, elapsed, min_duration_seconds)
        return
    alert_on_failure(source, exc)
