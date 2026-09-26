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

# «Стоп-кровь каналов» (2026-09-26, часть 1.1): раньше _rest_blocks() помечал
# delivered_in_digest=true СРАЗУ при чтении, до всякой попытки отправки —
# неудачный send() терял пункты навсегда и молча (классическая "тихая потеря
# данных", риск №1 проекта). Теперь: читаем БЕЗ пометки -> пытаемся отправить
# -> помечаем доставленными ТОЛЬКО при успехе. Неудача оставляет пункты
# pending — следующий прогон (завтра) заберёт их снова, ничего не потеряно.
# Счётчик подряд идущих неудач — в памяти процесса (как _last_write в
# run_log.py: сбрасывается при рестарте, это не риск потери данных, только
# риск чуть более поздней эскалации) — после FAILURE_ALERT_THRESHOLD подряд
# неудачных попыток шлём critical через alert_on_failure — ДРУГОЙ путь
# (err_dedup -> issue_log -> notify(critical), не голый notify._send() —
# если сам send() методично не работает несколько дней подряд, дайджест-канал
# явно ненадёжен, и рабочий путь эскалации не должен зависеть только от него).
FAILURE_ALERT_THRESHOLD = 3
_consecutive_failures = 0


def _pending_blocks(cur, day: str) -> list[tuple[int, str]]:
    """Всё, накопленное за день и ЕЩЁ НЕ доставленное (жёлтые аномалии, critical
    сверх бюджета, недельные/месячные отчёты в свой день, находки issue_review
    и т.п.) — в порядке накопления (ts). anamnesis/nutrition_reports сюда
    больше не попадают — у них свои каналы, см. докстринг модуля. Читает, но
    НЕ помечает доставленным — это делает _mark_delivered() после успешной
    отправки."""
    cur.execute(
        f"SELECT id, text FROM {schema()}.notify_log "
        "WHERE sent_date = %s AND immediate = false AND delivered_in_digest = false "
        "ORDER BY ts",
        (day,),
    )
    return [(r[0], r[1]) for r in cur.fetchall() if r[1]]


def _mark_delivered(ids: list[int]) -> None:
    if not ids:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {schema()}.notify_log SET delivered_in_digest = true WHERE id = ANY(%s)",
            (ids,),
        )
        conn.commit()


def build_and_send() -> dict:
    """Собирает и шлёт (если есть что). Возвращает {"sections": int, "sent": bool}
    — удобно для тестов, ничего не бросает наружу (обёрнуто в run_scheduler)."""
    global _consecutive_failures
    day = timeutil.today().isoformat()

    with get_conn() as conn, conn.cursor() as cur:
        pending = _pending_blocks(cur, day)

    if not pending:
        logger.info("digest: за %s копить нечего — не шлём", day)
        return {"sections": 0, "sent": False}

    ids = [row[0] for row in pending]
    combined = "\n\n———\n\n".join(row[1] for row in pending)
    ok = notify._send(combined)  # прямая единичная отправка — сам дайджест не через notify()
    # (иначе стал бы ещё одной "digest"-строкой в своём же журнале рекурсивно)

    if ok:
        _mark_delivered(ids)
        _consecutive_failures = 0
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {schema()}.notify_log (sent_date, source, priority, immediate, text) "
                "VALUES (%s, 'digest_sent', 'digest', true, %s)",
                (day, f"{len(pending)} секций"),
            )
            conn.commit()
    else:
        _consecutive_failures += 1
        logger.error("digest: отправка дайджеста за %s не удалась (%d подряд) — %d пунктов остаются pending",
                     day, _consecutive_failures, len(pending))
        if _consecutive_failures >= FAILURE_ALERT_THRESHOLD:
            alert_on_failure(
                "digest_delivery",
                RuntimeError(f"дайджест не отправляется {_consecutive_failures} раз(а) подряд, "
                            f"{len(pending)} пунктов копится в pending"),
            )
    return {"sections": len(pending), "sent": ok}


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
