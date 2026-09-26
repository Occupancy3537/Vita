"""Напоминание «панель созревает через 3 дня» (тикет «оптимизатор сдачи
анализов», 2026-09-26, Часть 3.3) — ежедневная проверка плана
(app/lab_optimizer.generate_plan), одна строка в дайджест НА ПАНЕЛЬ (не на
каждый маркер), плюс список для лаборатории (export_text панели уже готов).

Окно проверки — 0..3 дня до даты панели (не РОВНО 3): пропуск одного прогона
планировщика не должен тихо терять напоминание (тот же класс риска, что
описан в migrations/0002_lab_request.sql про card.lab_reminder_sent).
Дедуп — INSERT ... ON CONFLICT DO NOTHING в card.lab_reminder_sent(panel_date)
— ровно одно напоминание на дату панели, даже если план пересчитывается
(и меняется состав) между проверками."""
import logging
import time
from datetime import date

from psycopg import sql

from app import notify, run_log, timeutil
from app.db import get_conn, schema
from app.lab_optimizer import generate_plan
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHECK_HOUR_VL = 8
CHECK_MINUTE_VL = 30
REMINDER_WINDOW_DAYS = 3


def _already_sent(cur, panel_date_iso: str) -> bool:
    cur.execute(
        sql.SQL("SELECT 1 FROM {t} WHERE panel_date = %s").format(t=sql.Identifier(schema(), "lab_reminder_sent")),
        (panel_date_iso,),
    )
    return cur.fetchone() is not None


def _mark_sent(cur, panel_date_iso: str) -> bool:
    """True, если строка реально вставилась (значит, отправляем) — ON CONFLICT
    DO NOTHING делает это атомарным дедупом даже при гонке (не актуально при
    одном ежедневном планировщике, но дёшево сделать правильно сразу)."""
    cur.execute(
        sql.SQL("INSERT INTO {t} (panel_date) VALUES (%s) ON CONFLICT DO NOTHING")
        .format(t=sql.Identifier(schema(), "lab_reminder_sent")),
        (panel_date_iso,),
    )
    return cur.rowcount > 0


def run_once() -> dict:
    today = timeutil.now_local().date()
    with get_conn() as conn, conn.cursor() as cur:
        plan = generate_plan(cur, today=today)
        sent = 0
        for panel in plan["panels"]:
            panel_date = panel["date"]
            days_until = (date.fromisoformat(panel_date) - today).days
            if not (0 <= days_until <= REMINDER_WINDOW_DAYS):
                continue
            if not _mark_sent(cur, panel_date):
                continue  # уже отправляли на эту дату панели
            conn.commit()
            text = f"🧪 Панель созревает: {panel['export_text']}"
            notify.notify("lab_reminder", "normal", text)
            sent += 1
        conn.commit()
    logger.info("lab_reminder: проверено, отправлено напоминаний=%d", sent)
    return {"sent": sent}


def run_scheduler() -> None:
    logger.info("lab_reminder scheduler: старт (%02d:%02d ВЛ)", CHECK_HOUR_VL, CHECK_MINUTE_VL)
    while True:
        try:
            timeutil.sleep_until_local(CHECK_HOUR_VL, CHECK_MINUTE_VL)
            run_once()
            run_log.mark_run("lab_reminder")
        except Exception as e:
            logger.exception("lab_reminder run_once упал — повтор завтра")
            alert_on_failure("lab_reminder", e)
            time.sleep(3600)
