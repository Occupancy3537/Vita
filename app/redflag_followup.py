"""Follow-up по открытой сессии красного флага (бриф Влада, 2026-09-30).

Проблема: redflag_union записывает rf_event/rf_session, и после этого система
молчит — даже если пациент ни разу не вернулся к теме. Ровно одно поведение
добавляет этот модуль: сессия красного флага (L2/L3) открыта, 48 часов тишины —
ОДИН раз спросить Влада «как сейчас?». Пациент = Влад, единственный пользователь,
никаких уведомлений третьим лицам.

Правила (бриф, нарушение = баг):
  - только сессии, ОТКРЫТЫЕ ПОСЛЕ включения фичи: RF_FOLLOWUP_SINCE (ISO) —
    нижняя граница opened_ts. Пусто/некорректно = фича ничего не делает
    (никакого backfill: две старые открытые сессии от 15.09 не трогаются);
  - тишина = last_activity старше 48 часов (SESSION_SILENCE_HOURS);
  - уровни только L2/L3 (L1 — не тревога, closed — закрыт);
  - ОДНО сообщение на сессию: сначала доставка, и только при успехе — запись
    «отправлено» (card.rf_followup, migrations/0006). Неудача: sent_ts остаётся
    NULL, attempts+1, после MAX_ATTEMPTS=3 сессия больше не пытается;
  - доставка — СУЩЕСТВУЮЩАЯ doctor/intake._deliver_emergency (3 попытки +
    фолбэк на сервисный бот): переиспользуется как есть, её логика не тронута;
  - ответ Влада идёт обычным путём доктор-бота, отдельный разбор ответа не
    делается; новая rf_event по сессии обновляет last_activity в существующем
    record_rf_event — здесь на это реакции не нужно;
  - закрытие сессий не меняется.

run_once(cur, deliver, now=None) — чистая и тестируемая: соединение и
функция доставки введены параметрами (в тестах — кард_test-курсор и
фикстура-двойник; в проде run_scheduler подставляет реальные)."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from psycopg import sql

from app import run_log
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

# Влад, единственный пользователь (K5: чужие chat_id игнорируются по всей
# системе). Константа вместо env — тот же паттерн, что у err_dedup/health_watchdog.
CHAT_ID = "8956401"

SESSION_SILENCE_HOURS = 48
MAX_ATTEMPTS = 3
ALLOWED_LEVELS = ("L2", "L3")
RUN_EVERY_SECONDS = 3600  # раз в час

# Человекочитаемые названия категорий (бриф: «словарь рядом; неизвестная
# категория → общая формулировка, не сырой ключ»). Ключи — enum категорий
# союза: значения redflag.TAXONOMY_CATEGORY (там ОБРАТНЫЙ словарь: фраза →
# ключ) + systemic_warning из redflag_union. Новая категория без записи здесь
# не упадёт — уйдёт общая формулировка (дефолт ниже).
CATEGORY_NAMES = {
    "cardiac_acute": "боль в груди / одышка",
    "neuro_acute": "острая неврология (слабость, речь, зрение, головная боль)",
    "anaphylaxis": "анафилаксия / отёк, удушье",
    "psych_crisis": "тяжёлое психическое состояние",
    "bleeding_gi": "кровотечение из ЖКТ",
    "sepsis_suspect": "лихорадка со спутанностью / возможный сепсис",
    "severe_pain": "внезапная нестерпимая боль",
    "metabolic_acute": "острое метаболическое состояние",
    "systemic_warning": "общее системное предупреждение",
}
CATEGORY_DEFAULT = "твоё самочувствие"


def _since_epoch() -> Optional[datetime]:
    """RF_FOLLOWUP_SINCE (ISO) — нижняя граница opened_ts. Пусто или
    некорректно = None = фича ничего не делает (осознанный fail-closed:
    битая дата не должна ни отключать защиту от спама, ни срабатывать сама)."""
    raw = (os.environ.get("RF_FOLLOWUP_SINCE") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.error("RF_FOLLOWUP_SINCE=%r — не ISO-дата; follow-up выключен", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _category_text(category: str) -> str:
    return CATEGORY_NAMES.get(category, CATEGORY_DEFAULT)


def _message(category: str) -> str:
    return (f"Два дня назад ты писал про {_category_text(category)}. Как сейчас? "
            "Ответь парой слов — если стало хуже или повторилось, напиши сразу")


def run_once(cur, deliver: Callable[[str, Optional[int], str], bool], now=None) -> dict:
    """Один проход часового цикла. deliver — функция доставки в чат доктора
    (в проде doctor.intake._deliver_emergency; в тестах фикстура-двойник),
    контракт: (chat_id, message_id, text) -> bool, не бросает.

    Возвращает счётчики: considered (подошло под условия), sent, failed —
    и причину skipped, если фича выключена."""
    since = _since_epoch()
    if since is None:
        return {"skipped": "RF_FOLLOWUP_SINCE не задан или некорректен — фича выключена"}
    now = now or datetime.now(timezone.utc)
    silence_cutoff = now - timedelta(hours=SESSION_SILENCE_HOURS)

    cur.execute(
        sql.SQL(
            "SELECT s.id, s.category FROM {s} s "
            "LEFT JOIN {f} f ON f.session_id = s.id "
            "WHERE s.status = 'open' AND s.worst_level = ANY(%s) "
            "AND s.opened_ts >= %s AND s.last_activity < %s "
            "AND (f.session_id IS NULL OR (f.sent_ts IS NULL AND f.attempts < %s)) "
            "ORDER BY s.opened_ts"
        ).format(s=sql.Identifier(schema(), "rf_session"), f=sql.Identifier(schema(), "rf_followup")),
        (list(ALLOWED_LEVELS), since, silence_cutoff, MAX_ATTEMPTS),
    )
    rows = cur.fetchall()

    sent = failed = 0
    for session_id, category in rows:
        try:
            ok = deliver(CHAT_ID, None, _message(category))
            error = None if ok else "deliver вернул False"
        except Exception as e:  # defensive: контракт «не бросает», но двойник/сеть могут
            ok, error = False, repr(e)[:200]
        if ok:
            cur.execute(
                sql.SQL("SELECT attempts FROM {f} WHERE session_id = %s").format(
                    f=sql.Identifier(schema(), "rf_followup")),
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    sql.SQL("INSERT INTO {f} (session_id, sent_ts, attempts) VALUES (%s, %s, 1)").format(
                        f=sql.Identifier(schema(), "rf_followup")),
                    (session_id, now),
                )
            else:
                cur.execute(
                    sql.SQL("UPDATE {f} SET sent_ts = %s, attempts = %s, last_error = NULL WHERE session_id = %s").format(
                        f=sql.Identifier(schema(), "rf_followup")),
                    (now, row[0] + 1, session_id),
                )
            sent += 1
        else:
            cur.execute(
                sql.SQL("SELECT attempts FROM {f} WHERE session_id = %s").format(
                    f=sql.Identifier(schema(), "rf_followup")),
                (session_id,),
            )
            row = cur.fetchone()
            attempts = (row[0] if row else 0) + 1
            if row is None:
                cur.execute(
                    sql.SQL("INSERT INTO {f} (session_id, attempts, last_error) VALUES (%s, %s, %s)").format(
                        f=sql.Identifier(schema(), "rf_followup")),
                    (session_id, attempts, error),
                )
            else:
                cur.execute(
                    sql.SQL("UPDATE {f} SET attempts = %s, last_error = %s WHERE session_id = %s").format(
                        f=sql.Identifier(schema(), "rf_followup")),
                    (attempts, error, session_id),
                )
            failed += 1

    result = {"considered": len(rows), "sent": sent, "failed": failed}
    logger.info("redflag_followup: %s", result)
    return result


def run_scheduler() -> None:
    """Часовой цикл (бриф). Каркас — как lab_reminder.run_scheduler:
    alert_on_failure при падении + run_log.mark_run после успеха.
    Первый проход — сразу при старте, дальше раз в час."""
    logger.info("redflag_followup scheduler: старт (цикл %d c)", RUN_EVERY_SECONDS)
    # late import: Telegram-клиенты доктора не должны заводиться фактом импорта
    # этого модуля (например, в тестах без секретов) — только при реальном цикле.
    from app.doctor.intake import _deliver_emergency
    while True:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                run_once(cur, _deliver_emergency)
                conn.commit()
            run_log.mark_run("redflag_followup")
        except Exception as e:
            logger.exception("redflag_followup: прогон упал")
            alert_on_failure("redflag_followup", e)
        time.sleep(RUN_EVERY_SECONDS)
