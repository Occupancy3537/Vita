# -*- coding: utf-8 -*-
"""Замеры шагов (2026-10-01): раз в 5 минут смотрим health.live_steps_today (его обновляет intervals.icu-скрипт
garminbot каждые 20 минут) и, если updated_at новый, дописываем точку в card.steps_sample. Ключей intervals.icu
здесь нет и не нужно. Идемпотентно: PK по ts, повтор ничего не меняет."""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg import sql

from app import run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 300


def _live_row(cur):
    cur.execute("SELECT date, steps, updated_at FROM health.live_steps_today WHERE date = %s", (timeutil.today(),))
    return cur.fetchone()


def sample_once(cur) -> bool:
    """True — записана новая точка."""
    row = _live_row(cur)
    if not row or row[1] is None or row[2] is None:
        return False
    cur.execute(
        sql.SQL("INSERT INTO {t} (ts, date, steps) VALUES (%s, %s, %s) ON CONFLICT (ts) DO NOTHING")
        .format(t=sql.Identifier(schema(), "steps_sample")), (row[2], row[0], int(row[1])))
    return cur.rowcount == 1


def day_samples(cur, day) -> list[tuple[float, int]]:
    """[(локальный час, шаги)] за день по возрастанию времени."""
    cur.execute(sql.SQL("SELECT ts, steps FROM {t} WHERE date = %s ORDER BY ts")
                .format(t=sql.Identifier(schema(), "steps_sample")), (day,))
    tz = timeutil.person_tz()
    out = []
    for ts, steps in cur.fetchall():
        loc = ts.astimezone(tz)
        out.append((round(loc.hour + loc.minute / 60, 2), int(steps)))
    return out


def run_scheduler() -> None:
    logger.info("steps_sampler: старт (раз в %dс)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                if sample_once(cur):
                    conn.commit()
            run_log.mark_run("steps_sampler")
        except Exception as e:
            logger.exception("steps_sampler упал — повтор через %dс", CHECK_INTERVAL_SECONDS)
            alert_on_failure("steps_sampler", e)
        time.sleep(CHECK_INTERVAL_SECONDS)
