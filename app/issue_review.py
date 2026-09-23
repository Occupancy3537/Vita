"""Еженедельный разбор нерешённых находок card.issue_log (2026-09-23, по
прямому запросу Влада после реального инцидента: два ложных предупреждения
на «Настройках» вызвали тревогу без понятного действия — «эти ошибки нужно
либо автоматически чинить..., либо подсвечивать, что это критичная ошибка...
Остальные ошибки не должны меня отвлекать на экране настроек... спорные
моменты... выносятся 1 раз в неделю, остальное время они не должны
отображаться... и тригерить меня в телеграм»).

Модель трёх состояний находки (см. app/issue_log.py):
  - critical, открыта -> уже поднимается в верхний вердикт settings.html
    сразу (не ждёт недели — это и есть «срочно требует действий»).
  - important/minor, открыта, НЕ решена автоматикой -> копится тихо
    (виден только в фолде «Бэклог находок»), раз в неделю собирается сюда
    единым сообщением с вопросом «что делать».
  - snoozed/wontfix/fixed -> уже решённое, в дайджест не попадает вообще.

Дайджест НЕ шлётся, если решать нечего (пустой список) — тишина по
умолчанию: цель этого модуля уменьшить количество уведомлений, а не
завести ещё один канал, который всегда что-то пишет."""
import logging
import time
from datetime import date

from app import hermes_telegram as telegram  # алерты -> Hermes, не бот доктора
from app import run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
WEEKLY_HOUR_VL = 19  # воскресенье, за час до недельного советника (20:00) — не сливаются в одно сообщение


def _pick_undecided(cur) -> list[dict]:
    """Открытые находки, которые НЕ critical (те уже видны сразу в вердикте
    страницы — не нужно ждать неделю, чтобы их заметить)."""
    cur.execute(
        "SELECT natural_key, source, severity, summary, occurrences, first_seen "
        "FROM {t} WHERE status = 'open' AND severity != 'critical' "
        "ORDER BY first_seen".format(t=schema() + ".issue_log")
    )
    cols = ["natural_key", "source", "severity", "summary", "occurrences", "first_seen"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_digest_text(rows: list[dict]) -> str | None:
    """Чистая функция — текст дайджеста или None, если решать нечего."""
    if not rows:
        return None
    lines = [f"📋 Еженедельный разбор бэклога — {len(rows)} находок(и) ждут решения:\n"]
    for r in rows:
        age_days = (date.today() - r["first_seen"].date()).days if hasattr(r["first_seen"], "date") else "?"
        lines.append(
            f"• [{r['severity']}] {r['source']} ({r['occurrences']}×, {age_days} дн.)\n  {r['summary'][:200]}"
        )
    lines.append("\nОтветь по каждой: чинить / не трогать (snoozed) / никогда не чинить (wontfix).")
    return "\n".join(lines)


def run_once() -> int:
    """Возвращает число находок в дайджесте (0 = тишина, ничего не отправлено)."""
    with get_conn() as conn, conn.cursor() as cur:
        rows = _pick_undecided(cur)
    text = build_digest_text(rows)
    if text is None:
        logger.info("issue_review: бэклог решённых вопросов пуст — дайджест не отправлен")
        return 0
    telegram.send_message(CHAT_ID, text)
    return len(rows)


def _sleep_until(hour: int, minute: int = 0, weekday=None) -> None:
    timeutil.sleep_until_local(hour, minute, weekday=weekday)


def run_scheduler() -> None:
    logger.info("issue_review scheduler: старт (вс %02d:00 ВЛ)", WEEKLY_HOUR_VL)
    while True:
        try:
            _sleep_until(WEEKLY_HOUR_VL, 0, weekday=6)  # 6 = воскресенье (Python Monday=0)
            run_once()
            run_log.mark_run("issue_review")
        except Exception as e:
            logger.exception("issue_review: run_once упал — повтор через неделю")
            alert_on_failure("issue_review", e)
            time.sleep(3600)
