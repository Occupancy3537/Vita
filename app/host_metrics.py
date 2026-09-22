"""Коллектор метрик хоста (2026-09-22, страница «Настройки» на реальных данных).

Раз в 5 минут пишет load1/5/15 + память + swap в card.host_metrics. /proc
внутри контейнера показывает ХОСТОВЫЕ значения (без lxcfs load/meminfo/uptime
не виртуализируются — проверено живьём), поэтому отдельный сборщик на хосте не
нужен. История нужна для «пика за сутки»: /proc даёт только мгновенные
1/5/15-минутные средние, пик по ним не восстановить.

Реестр планировщиков, флаг и расписание — в app/system_status.py (страница).
"""
import logging
import time

from app import run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 5 * 60
KEEP_DAYS = 14


def _read_proc() -> dict | None:
    """(load1, load5, load15, mem_used_mb, swap_used_mb) из /proc хоста."""
    try:
        load1, load5, load15 = (float(x) for x in open("/proc/loadavg").read().split()[:3])
        mem: dict[str, int] = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])  # kB
        return {
            "load1": load1, "load5": load5, "load15": load15,
            "mem_used_mb": (mem["MemTotal"] - mem["MemAvailable"]) // 1024,
            "swap_used_mb": (mem["SwapTotal"] - mem["SwapFree"]) // 1024,
        }
    except Exception:
        logger.exception("host_metrics: не удалось прочитать /proc")
        return None


def run_once() -> None:
    m = _read_proc()
    if m is None:
        return
    with get_conn() as conn, conn.cursor() as cur:
        table = schema() + ".host_metrics"
        cur.execute(
            "INSERT INTO {t} (ts, load1, load5, load15, mem_used_mb, swap_used_mb) "
            "VALUES (now(), %s, %s, %s, %s, %s) ON CONFLICT (ts) DO NOTHING".format(t=table),
            (m["load1"], m["load5"], m["load15"], m["mem_used_mb"], m["swap_used_mb"]),
        )
        cur.execute("DELETE FROM {t} WHERE ts < now() - %s::interval".format(t=table),
                    ("%d days" % KEEP_DAYS,))
        conn.commit()
    run_log.mark_run("host_metrics")


def peak_since(cur, since_hours: int = 24) -> dict | None:
    """Пик load1 за окно (время — в зоне человека через timeutil)."""
    cur.execute(
        "SELECT ts, load1 FROM {t} WHERE ts > now() - %s::interval "
        "ORDER BY load1 DESC NULLS LAST LIMIT 1".format(t=schema() + ".host_metrics"),
        ("%d hours" % since_hours,),
    )
    row = cur.fetchone()
    if not row or row[1] is None:
        return None
    local = row[0].astimezone(timeutil.person_tz())
    return {"at": local.strftime("%H:%M"), "load": float(row[1])}


def run_scheduler() -> None:
    logger.info("host_metrics scheduler: старт (каждые %d мин)", INTERVAL_SECONDS // 60)
    while True:
        try:
            run_once()
        except Exception as e:
            logger.exception("host_metrics: run_once упал — повтор через обычный интервал")
            alert_on_failure("host_metrics", e)
        time.sleep(INTERVAL_SECONDS)
