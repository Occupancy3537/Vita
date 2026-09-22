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

from app import run_log
from app.db import get_conn
from app.err_dedup import EXPECTED_TOKEN, run_notify

logger = logging.getLogger(__name__)


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


def alert_on_sustained_failure(source: str, exc: BaseException, consecutive_failures: int, threshold: int = 3) -> None:
    """Как alert_on_failure(), но только когда сбои идут ПОДРЯД `threshold` раз
    и больше (2026-09-23, по прямому запросу Влада: «система должна сама
    отлавливать ошибки, которые ничего не сломают» — единичный сетевой обрыв
    long-polling ("[Errno 104] Connection reset by peer" и подобные) НИЧЕГО
    не ломает: retry-цикл сам переживает его за секунды, ничего не потеряно.
    До этой функции КАЖДЫЙ такой обрыв всё равно шёл полным alert_on_failure —
    Telegram-алерт (пока не подавлен 60-мин дедупом), запись в
    card.scheduler_run_log (видна на «Настройках»), запись в card.issue_log
    (Шаг 1 «петли самоулучшения») — три источника шума на то, что само
    прошло за 5 секунд. Ниже порога — ни один из этих трёх следов не
    появляется вообще, поднимать шум стоит, только когда сбои реально не
    проходят сами."""
    if consecutive_failures < threshold:
        logger.info("%s: сбой %s/%s подряд (%s) — транзиентно, не алерчу", source, consecutive_failures, threshold, exc)
        return
    alert_on_failure(source, exc)
