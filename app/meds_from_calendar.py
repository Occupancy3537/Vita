"""Порт n8n `Card: Meds from Calendar` (2026-09-21, последний пункт группы 1
— 4 ноды, самый маленький порт всей миграции). Ежедневно 09:00 ВЛ читает
сегодняшние события Google-календаря, отбирает похожие на приём лекарства/
добавки (единица дозировки в названии, либо слово "таблетка/капсула/доза"),
синхронизирует каждое в card-service как intervention (kind='supplement').

Credential (Google Calendar, PqacoYjKhCXLSGf8) уже расшифрован и используется
с #34 (app/sheets_client.py, порт Collect_Biohacking_Data) — тот же клиент,
новых credentials заводить не пришлось. `POST /interventions/sync` — уже
существующий эндпоинт card-service (idempotent по source_ref), здесь просто
прямой вызов в процессе вместо HTTP-круга на самого себя, тот же паттерн,
что и все остальные "n8n звал card-service по HTTP" порты этой сессии."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import run_log
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

VL = timezone(timedelta(hours=10))
DAILY_HOUR_VL = 9
CALENDAR_ID = "vasukvladislav@gmail.com"

# Порт MED_RE 1:1 из "Filter Medication-like".
MED_RX = re.compile(
    r"\d+(\.\d+)?\s*(mg|mcg|iu)\b|\d+(\.\d+)?\s*(ме|мкг|мг)(?![а-яёa-z])|таблетк|капсул|доз[аыу]",
    re.I,
)


def filter_medication_like(events: list[dict]) -> list[dict]:
    """Порт "Filter Medication-like". `events` — Google Calendar events.list
    items (те же, что app.sheets_client.get_calendar_events отдаёт)."""
    out = []
    for j in events:
        summary = j.get("summary") or ""
        if not MED_RX.search(summary):
            continue
        start = j.get("start") or {}
        started_ts = start.get("dateTime") or start.get("date")
        out.append({
            "name": summary,
            "source_ref": j.get("recurringEventId") or j.get("id"),
            "started_ts": started_ts,
        })
    return out


def sync_meds_from_calendar(events: list[dict]) -> int:
    """Порт "Sync to card-service" — прямой вызов вместо HTTP POST на себя же.
    Возвращает количество успешно синхронизированных событий."""
    from app.main import InterventionSyncRequest, interventions_sync

    synced = 0
    for m in filter_medication_like(events):
        if not m["source_ref"]:
            continue
        try:
            req = InterventionSyncRequest(
                name=m["name"], source_ref=m["source_ref"], kind="supplement",
                started_ts=m["started_ts"], origin="calendar",
            )
            interventions_sync(req)
            synced += 1
        except Exception:
            logger.exception("meds_from_calendar: не удалось синхронизировать %r", m["name"])
    return synced


def run_once() -> None:
    from app.sheets_client import get_calendar_events

    now = datetime.now(VL)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    try:
        events = get_calendar_events(
            CALENDAR_ID,
            start_of_day.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            end_of_day.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except Exception:
        logger.exception("meds_from_calendar: Google Calendar недоступен, пропускаю сегодняшний прогон")
        return

    n = sync_meds_from_calendar(events)
    logger.info("meds_from_calendar: синхронизировано %d событий из %d за сегодня", n, len(events))


def _sleep_until(hour: int, minute: int = 0) -> None:
    now = datetime.now(VL)
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def run_scheduler() -> None:
    logger.info("meds_from_calendar scheduler: старт (%02d:00 ВЛ)", DAILY_HOUR_VL)
    while True:
        try:
            _sleep_until(DAILY_HOUR_VL)
            run_once()
            run_log.mark_run("meds_from_calendar")
        except Exception as e:
            logger.exception("meds_from_calendar: run_once упал — повтор завтра")
            alert_on_failure("meds_from_calendar", e)
            time.sleep(3600)
