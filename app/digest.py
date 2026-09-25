"""Вечерний дайджест (ROADMAP 5.5, 2026-09-24; сужен тем же днём тикетом
«раскладка ботов по тематическим чатам») — одно сообщение сервисным ботом
вместо потока: всё, что накопилось за день через app/notify.py (priority
normal/digest, и critical сверх дневного бюджета — не теряется, просто не
срочно). Пустой дайджест не отправляется вовсе.

Раньше (ROADMAP 5.5, тем же днём) сюда ещё заходили анамнез первым блоком и
сводка питания вторым — первые живые сутки показали, что это неудобно
(анамнез внутри общего сообщения, конфликт бота с личным ИИ-агентом Влада).
Оба переехали в свои тематические чаты со своим временем и больше НЕ идут
через notify()/этот дайджест: анамнез — app/doctor/anamnesis.py::run_scheduler
(11:00, чат доктора), сводка питания — app/nutrition_reports.py::run_daily
(21:45, чат дневника питания). Здесь остаётся ровно то, что реально
общее/сервисное: жёлтые аномалии, weekly/monthly-отчёты, critical сверх
бюджета, находки issue_review и т.п."""
import logging
import time

from app import notify, run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

DIGEST_HOUR_VL = 21
DIGEST_MINUTE_VL = 50


def _rest_blocks(cur, day: str) -> list[str]:
    """Всё, накопленное за день (жёлтые аномалии, critical сверх бюджета,
    недельные/месячные отчёты в свой день, находки issue_review и т.п.) —
    в порядке накопления (ts). anamnesis/nutrition_reports сюда больше не
    попадают — у них свои каналы, см. докстринг модуля."""
    cur.execute(
        f"SELECT id, text FROM {schema()}.notify_log "
        "WHERE sent_date = %s AND immediate = false AND delivered_in_digest = false "
        "ORDER BY ts",
        (day,),
    )
    rows = cur.fetchall()
    if rows:
        cur.execute(
            f"UPDATE {schema()}.notify_log SET delivered_in_digest = true "
            "WHERE id = ANY(%s)",
            ([r[0] for r in rows],),
        )
    return [r[1] for r in rows if r[1]]


def build_and_send() -> dict:
    """Собирает и шлёт (если есть что). Возвращает {"sections": int, "sent": bool}
    — удобно для тестов, ничего не бросает наружу (обёрнуто в run_scheduler)."""
    day = timeutil.today().isoformat()
    sections: list[str] = []

    # всё, что скопилось за день (anamnesis/nutrition_reports больше не сюда —
    # см. докстринг модуля).
    with get_conn() as conn, conn.cursor() as cur:
        rest = _rest_blocks(cur, day)
        conn.commit()
    sections.extend(rest)

    if not sections:
        logger.info("digest: за %s копить нечего — не шлём", day)
        return {"sections": 0, "sent": False}

    combined = "\n\n———\n\n".join(sections)
    ok = notify._send(combined)  # прямая единичная отправка — сам дайджест не через notify()
    # (иначе стал бы ещё одной "digest"-строкой в своём же журнале рекурсивно)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.notify_log (sent_date, source, priority, immediate, text) "
            "VALUES (%s, 'digest_sent', 'digest', true, %s)",
            (day, f"{len(sections)} секций"),
        )
        conn.commit()
    if not ok:
        logger.error("digest: отправка дайджеста за %s не удалась", day)
    return {"sections": len(sections), "sent": ok}


def run_scheduler() -> None:
    logger.info("digest scheduler: старт (%02d:%02d ВЛ)", DIGEST_HOUR_VL, DIGEST_MINUTE_VL)
    while True:
        try:
            timeutil.sleep_until_local(DIGEST_HOUR_VL, DIGEST_MINUTE_VL)
            build_and_send()
            run_log.mark_run("digest")
        except Exception as e:
            logger.exception("digest scheduler упал — повтор завтра")
            alert_on_failure("digest", e)
            time.sleep(3600)
