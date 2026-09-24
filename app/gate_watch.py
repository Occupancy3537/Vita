"""Алерт на снятие/возврат гейта нагрузки — порт из n8n Build Today JSON
(2026-09-20, A6, ревью Opus 5 2026-09-09: снятие мед-ограничения обязано быть
шумным, независимо от причины — снял руками / сбой синка / regex промахнулся).

В оригинале это делал сам Code node на каждом тике 15-минутного Schedule
Trigger — сравнивал decision.gate.blocked с предыдущим значением в
$getWorkflowStaticData (переживает рестарт n8n, хранится в БД вместе с
воркфлоу). get_today_dashboard() в card-service — чистая функция без
состояния (как get_bioage_dashboard/get_health_dashboard), поэтому переход
между тиками хранится отдельно здесь, в health.gate_state (1 строка,
переживает рестарт card-service — в отличие от переменной в памяти процесса).

Интервал 15 минут — тот же, что был у исходного Schedule Trigger."""
import logging
import time

from app.dashboard import get_today_dashboard
from app.db import get_conn
from app import notify
from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
INTERVAL_SECONDS = 15 * 60


def _last_known_blocked(cur):
    cur.execute("SELECT blocked FROM health.gate_state WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def _store_blocked(cur, blocked: bool) -> None:
    cur.execute(
        "INSERT INTO health.gate_state (id, blocked, updated_at) VALUES (1, %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET blocked = EXCLUDED.blocked, updated_at = now()",
        (blocked,),
    )


def check_once() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        today = get_today_dashboard(cur)
        gate = (today.get("decision") or {}).get("gate") or {}
        now_blocked = bool(gate.get("blocked"))
        prev_blocked = _last_known_blocked(cur)

        # prev_blocked is None только на самом первом тике после создания
        # таблицы — не алертим на переход, которого не видели (нет базы для
        # сравнения), просто запоминаем текущее состояние.
        if prev_blocked is True and now_blocked is False:
            notify.notify(
                "gate_watch", "critical",
                "🟢➡️ <b>Гейт нагрузки СНЯТ</b>\n\n"
                "Было ограничение по спине, сейчас — нет. "
                f"Источник сейчас: {gate.get('source') or 'нет'}.\n\n"
                "Если ты это НЕ менял намеренно — проверь health.patient_state "
                "(возможно, не синхронизировался или перезаписан).",
                parse_mode="HTML",
            )
        elif prev_blocked is False and now_blocked is True:
            notify.notify(
                "gate_watch", "critical",
                f"ℹ️ Гейт нагрузки снова активен (источник: {gate.get('source') or '?'}).",
                parse_mode="HTML",
            )

        _store_blocked(cur, now_blocked)
        conn.commit()


def run_scheduler() -> None:
    """Тот же паттерн, что app.doctor.anamnesis/app.system_check: цикл со сном,
    сбой одного тика не убивает поток."""
    logger.info("gate_watch scheduler: старт")
    while True:
        try:
            check_once()
            run_log.mark_run("gate_watch")
        except Exception as e:
            logger.exception("gate_watch check_once упал — повтор через %ss", INTERVAL_SECONDS)
            alert_on_failure("gate_watch", e)
        time.sleep(INTERVAL_SECONDS)
