"""Follow-up по открытым сессиям красных флагов L2/L3 (2026-09-30).

Проблема: красный флаг детектируется, rf_event записывается, rf_session
открывается — и после этого система молчит. Пациент может забыть о симптоме
или не понять его серьёзность.

Решение: раз в час планировщик ищет открытые сессии L2/L3, у которых
последняя активность >48 часов, и ОДИН раз спрашивает Влада «как сейчас?».

Правила:
- только сессии НОВЕЕ RF_FOLLOWUP_SINCE (env, ISO datetime; пусто/не задано =
  фича выключена — никаких backfill на старых сессиях); naive datetime
  интерпретируется как UTC;
- только worst_level L2/L3 (L1 — заметка на будущее, не разговор);
- отправка через doctor/intake._deliver_emergency (3 попытки + фолбэк на
  сервисный бот — эту логику НЕ трогаем и НЕ дублируем);
- идемпотентно через card.rf_followup: одна строка на сессию; повторная
  попытка = только если sent_ts IS NULL И attempts < MAX_ATTEMPTS (сбой
  доставки не теряет вопрос — пробуем в следующий час);
- закрытие сессий не меняется (redflag_union делает это по 48ч тишине);
- ответ Влада идёт обычным путём доктор-бота — отдельный разбор ответа не нужен.

Миграция: migrations/0006_rf_followup.sql (применяет Claude)."""
import logging
import os
from datetime import datetime, timedelta, timezone

from psycopg import sql

from app import run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600
STALE_HOURS = 48
MAX_ATTEMPTS = 3

OWNER_CHAT_ID = "8956401"

CATEGORY_RU = {
    "cardiac_acute": "сердце",
    "neuro_acute": "неврологические симптомы",
    "anaphylaxis": "аллергическая реакция",
    "psych_crisis": "психологический кризис",
    "bleeding_gi": "кровотечение",
    "sepsis_suspect": "возможная инфекция",
    "severe_pain": "сильная боль",
    "metabolic_acute": "метаболические нарушения",
    "systemic_warning": "общее недомогание",
}

FOLLOWUP_TEMPLATE = (
    "Недавно ты писал про {topic}. "
    "Как сейчас? Ответь парой слов — если стало хуже или повторилось, напиши сразу."
)
FOLLOWUP_FALLBACK_TOPIC = "симптом из красного флага"


def _since() -> datetime | None:
    raw = os.environ.get("RF_FOLLOWUP_SINCE", "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("redflag_followup: невалидный RF_FOLLOWUP_SINCE=%r — фича выключена", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _topic_ru(category: str) -> str:
    return CATEGORY_RU.get(category, FOLLOWUP_FALLBACK_TOPIC)


def _get_deliver():
    from app.doctor.intake import _deliver_emergency
    return _deliver_emergency


def run_once(now: datetime | None = None) -> dict:
    since = _since()
    if since is None:
        return {"sent": 0, "failed": 0, "skipped_no_since": True}

    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=STALE_HOURS)
    sent = failed = 0

    deliver = _get_deliver()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT s.id, s.category, s.last_activity "
                "FROM {rf_session} s "
                "WHERE s.status = 'open' "
                "  AND s.worst_level IN ('L2', 'L3') "
                "  AND s.opened_ts >= %s "
                "  AND s.last_activity < %s "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM {rf_followup} f "
                "      WHERE f.session_id = s.id "
                "        AND (f.sent_ts IS NOT NULL OR f.attempts >= %s)"
                "  ) "
                "ORDER BY s.last_activity"
            ).format(
                rf_session=sql.Identifier(schema(), "rf_session"),
                rf_followup=sql.Identifier(schema(), "rf_followup"),
            ),
            (since, stale_cutoff, MAX_ATTEMPTS),
        )
        candidates = cur.fetchall()

    for session_id, category, last_activity in candidates:
        topic = _topic_ru(category)
        text = FOLLOWUP_TEMPLATE.format(topic=topic)

        delivered = deliver(OWNER_CHAT_ID, None, text)

        with get_conn() as conn, conn.cursor() as cur:
            if delivered:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {t} (session_id, sent_ts, attempts) "
                        "VALUES (%s, now(), 1) "
                        "ON CONFLICT (session_id) DO UPDATE SET "
                        "  sent_ts = now(), attempts = {t}.attempts + 1"
                    ).format(t=sql.Identifier(schema(), "rf_followup")),
                    (session_id,),
                )
                sent += 1
                logger.info("redflag_followup: follow-up отправлен по сессии %s (%s)",
                            session_id, category)
            else:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {t} (session_id, attempts, last_error) "
                        "VALUES (%s, 1, 'delivery failed') "
                        "ON CONFLICT (session_id) DO UPDATE SET "
                        "  attempts = {t}.attempts + 1, last_error = 'delivery failed'"
                    ).format(t=sql.Identifier(schema(), "rf_followup")),
                    (session_id,),
                )
                failed += 1
                logger.warning("redflag_followup: доставка не удалась по сессии %s",
                               session_id)
            conn.commit()

    if sent or failed:
        logger.info("redflag_followup: отправлено=%d, не удалось=%d", sent, failed)
    return {"sent": sent, "failed": failed, "skipped_no_since": False}


def run_scheduler() -> None:
    import time
    logger.info("redflag_followup scheduler: старт (раз в %dс)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            run_once()
            run_log.mark_run("redflag_followup")
        except Exception as e:
            logger.exception("redflag_followup run_once упал — повтор через %dс",
                             CHECK_INTERVAL_SECONDS)
            alert_on_failure("redflag_followup", e)
        time.sleep(CHECK_INTERVAL_SECONDS)
