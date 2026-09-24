"""Порт n8n `_Memory Pre-Archive Check` (2026-09-20, группа малых утилит).

Оригинал был тонким планировщиком поверх УЖЕ существующего card-service
эндпоинта `/health-check/pre-archive` (`run_pre_archive_check`, П4 §6.2) —
n8n здесь не считал ничего сам, только раз в сутки дёргал HTTP и форматировал
алерт. Раз вызывающий и вызываемый — один и тот же процесс, лишний HTTP-круг
через `http://card-service:8080/...` убран: зовём `run_pre_archive_check(cur)`
напрямую.

w3_question — эпизод с критическим паттерном, молчавший 90+ дней: тихо забыть
нельзя, нужен ответ Влада. "archived" — не шумим (см. run_pre_archive_check)."""
import logging
import time

from app.db import get_conn
from app import notify
from app.memory import run_pre_archive_check
from app import run_log, timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
CHECK_HOUR_VL = 8


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = abs(n) % 10, abs(n) % 100
    if 10 < n100 < 20:
        return many
    if 1 < n10 < 5:
        return few
    if n10 == 1:
        return one
    return many


def build_alert(cur) -> str:
    items = run_pre_archive_check(cur)
    w3 = [i for i in items if i.get("action") == "w3_question"]
    if not w3:
        return ""
    lines = [f"• {i.get('symptom_key') or '?'} (id {i['episode_id']}) — {i['reason']}" for i in w3]
    word = _plural_ru(len(w3), "эпизод", "эпизода", "эпизодов")
    return (
        f"🗂 Память: {len(w3)} {word} с критическим паттерном молчат 90+ дней. "
        "Тихо архивировать нельзя — подтверди, что можно забыть:\n\n" + "\n".join(lines)
    )


def _build_alert() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        return build_alert(cur)


def run_once() -> None:
    alert = _build_alert()
    if alert:
        notify.notify("memory_archive_check", "normal", alert, parse_mode="HTML")


def run_scheduler() -> None:
    """Тот же паттерн, что app.system_check/app.doctor.anamnesis: раз в сутки
    в CHECK_HOUR_VL, сбой одного тика не убивает поток."""
    logger.info("memory_archive_check scheduler: старт")
    while True:
        try:
            timeutil.sleep_until_local(CHECK_HOUR_VL)  # Фаза 3: по поясу человека
            run_once()
            run_log.mark_run("memory_archive_check")
        except Exception as e:
            logger.exception("memory_archive_check run_once упал — повтор завтра")
            alert_on_failure("memory_archive_check", e)
            time.sleep(3600)
