"""Вечерний дайджест (ROADMAP 5.5, 2026-09-24) — одно сообщение вместо
потока: вопрос анамнеза первым блоком, сводка питания вторым, дальше —
всё, что накопилось за день через app/notify.py (priority normal/digest,
и critical сверх дневного бюджета — не теряется, просто не срочно).
Пустой дайджест не отправляется вовсе.

Порядок: анамнеза/питание — ФИКСИРОВАННЫЕ первые два блока (не по времени
постановки в очередь — nutrition_reports ставится в очередь позже анамнеза,
но должен идти вторым, не последним), всё остальное — в порядке накопления
за день (ts).

21:50, не 21:45: nutrition_reports.run_daily_scheduler() тоже стреляет в
21:45 (не трогали её время специально — она и раньше была "вечерним
отчётом", теперь просто копит вместо отправки) — 5 минут запаса гарантируют,
что её пункт уже в card.notify_log к моменту сборки."""
import logging
import time

from app import notify, run_log, timeutil
from app.db import get_conn, schema
from app.doctor import anamnesis
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

DIGEST_HOUR_VL = 21
DIGEST_MINUTE_VL = 50


def _fixed_block(cur, day: str, source: str) -> str | None:
    """Один источник -> одна секция (первая строка за сегодня, если их
    почему-то несколько). Помечает delivered_in_digest, чтобы не попасть
    ещё раз в "всё остальное" ниже."""
    cur.execute(
        f"SELECT id, text FROM {schema()}.notify_log "
        "WHERE sent_date = %s AND source = %s AND immediate = false AND delivered_in_digest = false "
        "ORDER BY ts LIMIT 1",
        (day, source),
    )
    row = cur.fetchone()
    if not row or not row[1]:
        return None
    cur.execute(f"UPDATE {schema()}.notify_log SET delivered_in_digest = true WHERE id = %s", (row[0],))
    return row[1]


def _rest_blocks(cur, day: str) -> list[str]:
    """Всё остальное, накопленное за день (жёлтые аномалии, critical сверх
    бюджета, недельные/месячные отчёты в свой день, находки issue_review
    и т.п.) — в порядке накопления, кроме уже забранных фиксированных блоков
    выше (nutrition_reports; anamnesis никогда сюда не попадает — не через
    notify(), см. build_and_send)."""
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

    # 1. вопрос анамнеза — ПЕРВЫМ блоком, не через notify() (фиксированная
    #    позиция, не порядок по времени постановки в очередь).
    try:
        anam_res = anamnesis.ask_daily()
        if anam_res.get("action") == "ask" and anam_res.get("text"):
            sections.append(anam_res["text"])
    except Exception:
        logger.exception("digest: anamnesis.ask_daily() упал — блок анамнеза пропущен")

    # 2. сводка питания — ВТОРЫМ блоком (nutrition_reports сам кладёт текст
    #    в notify_log на своём обычном 21:45-триггере, см. app/nutrition_reports.py).
    with get_conn() as conn, conn.cursor() as cur:
        nutrition_text = _fixed_block(cur, day, "nutrition_reports")
        conn.commit()
    if nutrition_text:
        sections.append(nutrition_text)

    # 3. всё остальное, что скопилось за день.
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
