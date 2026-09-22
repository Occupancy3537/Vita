"""Журнал прогонов фоновых циклов (2026-09-22, страница «Настройки» на реальных
данных; закрывает и находку аудита «сбой фонового цикла уходит только в docker
logs, которые проактивно никто не читает»).

До этого модуля у 19 циклов не было никакого следа успешных прогонов: ошибки
шли в Telegram-алерты через alert_on_failure (с дедупом), но факт «цикл жив и
отработал N минут назад» нигде не хранился; docker-логи при редеплое (docker
rm -f) теряются. Теперь каждый цикл отмечает успех (mark_run) и ошибку
(mark_error), страница /dashboard/system-status читает отсюда.

Fail-safe осознанный: журналирование НИКОГДА не бросает — наблюдаемость не
должна ломать сам цикл (тот же принцип, что у alert_on_failure).
"""
import logging
import time

from app.db import get_conn, schema

logger = logging.getLogger(__name__)

# Троттлинг для «сердцебиений» (поллеры зовут mark_run каждые ~30с, писать это
# в БД незачем — раз в 5 минут достаточно, чтобы отличить «жив» от «умер»).
_last_write: dict[str, float] = {}


def mark_run(name: str, min_interval_seconds: float = 0.0) -> None:
    """Отметить успешный прогон цикла name. min_interval_seconds>0 — не чаще,
    чем раз в N секунд (для поллеров; состояние троттлинга — в памяти
    процесса, при рестарте сбрасывается, это допустимо)."""
    if min_interval_seconds:
        now_mono = time.monotonic()
        if now_mono - _last_write.get(name, 0.0) < min_interval_seconds:
            return
        _last_write[name] = now_mono
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO {t} (name, last_ok_at) VALUES (%s, now()) "
                "ON CONFLICT (name) DO UPDATE SET last_ok_at = now()".format(t=schema() + ".scheduler_run_log"),
                (name,),
            )
            conn.commit()
    except Exception:
        logger.warning("run_log: не удалось отметить прогон %r", name, exc_info=True)


def mark_error(name: str, exc: BaseException) -> None:
    """Записать последнюю ошибку цикла. Текст ошибки НЕ стирается успешным
    прогоном — странице важно видеть «ошибка была тогда-то, потом ок»."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO {t} (name, last_error, last_error_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET last_error = EXCLUDED.last_error, "
                "last_error_at = now()".format(t=schema() + ".scheduler_run_log"),
                (name, str(exc)[:500]),
            )
            conn.commit()
    except Exception:
        logger.warning("run_log: не удалось записать ошибку %r", name, exc_info=True)
