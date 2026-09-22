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
