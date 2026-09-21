"""Порт n8n `Get Yandex Climate_2` (2026-09-21, найден и перенесён при
проверке «можно ли полностью убрать n8n») — ежечасный (в :05) сбор датчика
климата в спальне через Yandex Smart Home API: «будим» устройство (POST
devices/actions on=true — без этого сенсор иногда отдаёт кэш вместо свежих
показаний), ждём 5с, читаем состояние, парсим temperature/humidity/
pm2.5_density из properties[], дописываем строку в Google Sheets
MicroClimate (тот же лист, что app.biohacking_ingest.py читает построчно
для усреднения climate по часам сна — см. его докстринг про то, почему
health.microclimate — дневной агрегат — для этого не годится)."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

VL = timezone(timedelta(hours=10))
HOURLY_MINUTE_VL = 5

YANDEX_DEVICE_ID = "1c92a9dc-cd69-448a-98ba-f202814f50b0"
YANDEX_API_BASE = "https://api.iot.yandex.net/v1.0"

MICROCLIMATE_SHEET_ID = "1wejYO7GKnizGQ9QIMzPua4NxY8uGKwRIcJxBDvyfcqw"
MICROCLIMATE_SHEET_TITLE = "Лист1"


def _auth_header() -> dict:
    token = os.environ.get("YANDEX_IOT_TOKEN", "")
    if not token:
        raise RuntimeError("YANDEX_IOT_TOKEN не задан")
    return {"Authorization": token}


def wake_device(timeout: float = 15.0) -> None:
    """Порт "Wake Up Cache (Yandex API)"."""
    httpx.post(
        f"{YANDEX_API_BASE}/devices/actions",
        headers=_auth_header(),
        json={"devices": [{"id": YANDEX_DEVICE_ID, "actions": [
            {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}},
        ]}]},
        timeout=timeout,
    ).raise_for_status()


def get_device_state(timeout: float = 15.0) -> dict:
    """Порт "Get Yandex Climate"."""
    resp = httpx.get(f"{YANDEX_API_BASE}/devices/{YANDEX_DEVICE_ID}", headers=_auth_header(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_climate(device: dict, now: str = None) -> dict:
    """Порт "Parse Climate" — вытаскивает temperature/humidity/pm2.5_density
    из properties[]. `now` — для тестов (иначе datetime.now(timezone.utc))."""
    temp = hum = pm25 = None
    for prop in device.get("properties") or []:
        params = prop.get("parameters") or {}
        state = prop.get("state") or {}
        if state.get("value") is None:
            continue
        instance = params.get("instance")
        if instance == "temperature":
            temp = state["value"]
        elif instance == "humidity":
            hum = state["value"]
        elif instance == "pm2.5_density":
            pm25 = state["value"]
    return {
        "Дата": now or datetime.now(timezone.utc).isoformat(),
        "Температура": temp, "Влажность": hum, "PM2.5": pm25,
    }


def sync_to_sheet(row: dict) -> None:
    from app.sheets_client import append_row
    append_row(MICROCLIMATE_SHEET_ID, MICROCLIMATE_SHEET_TITLE,
               [row["Дата"], row["Температура"], row["Влажность"], row["PM2.5"]])


def run_once() -> None:
    try:
        wake_device()
    except Exception:
        logger.exception("yandex_climate: не удалось разбудить устройство — пробую прочитать состояние как есть")
    time.sleep(5)
    try:
        device = get_device_state()
    except Exception:
        logger.exception("yandex_climate: Yandex API недоступен, пропускаю этот час")
        return
    row = parse_climate(device)
    # ОСОЗНАННОЕ отличие от оригинала: он писал строку в Sheets даже если все
    # три значения null (устройство не ответило свойствами) — здесь такая
    # пустая строка не пишется вообще, чтобы не засорять лист шумом. Если
    # нужно 1:1 поведение (видеть даже "устройство молчало"), скажи — уберу.
    if row["Температура"] is None and row["Влажность"] is None and row["PM2.5"] is None:
        logger.warning("yandex_climate: устройство не отдало ни одного значения, не пишу пустую строку")
        return
    try:
        sync_to_sheet(row)
    except Exception:
        logger.exception("yandex_climate: не удалось записать в Google Sheets")


def _sleep_until_next_hour(minute: int) -> None:
    now = datetime.now(VL)
    nxt = now.replace(minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def run_scheduler() -> None:
    logger.info("yandex_climate scheduler: старт (каждый час, :%02d)", HOURLY_MINUTE_VL)
    while True:
        try:
            _sleep_until_next_hour(HOURLY_MINUTE_VL)
            run_once()
        except Exception:
            logger.exception("yandex_climate: run_once упал — повтор через час")
            time.sleep(3600)
