"""Единственная точка ИНИЦИАТИВНОЙ отправки (ROADMAP 5.1, 2026-09-24).

Контекст: раньше каждый фоновый цикл слал в Telegram напрямую, когда хотел —
даже "✅ Система в норме" ежедневно и отчёт о питании каждый вечер, гарантируя
минимум 2-3 сообщения в день, даже когда всё в порядке. Красные флаги
терялись в этом же потоке — а это safety-система. Цель: обычный день —
0-2 немедленных сообщения + один вечерний дайджест (app/digest.py).

НЕ через этот модуль — диалоговые ответы: доктор и дневник питания отвечают
на СООБЩЕНИЕ Влада (это разговор, не инициатива, бюджета не касается), так
же registrar.py (подтверждение после загрузки лаб-документа — та же прямая
реакция на его действие). Единственное исключение в другую сторону — три
слоя красных флагов + гейт: они инициативны по смыслу (система САМА решает,
что нужно вмешаться), поэтому подключены сюда с priority="red_flag".

Приоритеты:
  red_flag — ВСЕГДА немедленно, ВНЕ дневного бюджета (безопасность важнее
             тишины). Только для _deliver_emergency (doctor/intake.py — свои
             3 попытки + фолбэк Hermes, эту логику НЕ трогаем и НЕ дублируем
             здесь — она логируется через log_external_send(), не notify())
             и write_path._alert_owner_redflag (обычный одиночный send —
             мигрирован на notify() целиком).
  critical — немедленно, ПОКА дневной бюджет (CRITICAL_DAILY_BUDGET) не
             исчерпан; сверх бюджета — молча уходит в вечерний дайджест
             как обычный пункт (не теряется, просто не срочно).
  normal / digest — никогда не шлётся сразу, всегда копится на вечер.
             Разницы в обработке нет — оба имени просто читаемость на
             стороне вызывающего ("это фоновая сводка" vs "это и задумано
             как дайджест-материал, например недельный отчёт").

Сутки — по timeutil.today() (человек, не UTC/сервер), как везде в проекте.
Журнал — card.notify_log, читает и пишет и бюджет, и сборщик дайджеста
(app/digest.py) — единственный источник правды, не два разных состояния."""
import logging

from app import hermes_telegram, timeutil
from app.db import get_conn, schema

logger = logging.getLogger(__name__)

CRITICAL_DAILY_BUDGET = 2

PRIORITIES = ("red_flag", "critical", "normal", "digest")


def _send(text: str, parse_mode: str | None = None) -> bool:
    try:
        hermes_telegram.send_message(hermes_telegram.CHAT_ID, text, parse_mode=parse_mode)
        return True
    except Exception:
        logger.exception("notify: отправка не удалась")
        return False


def _count_immediate_critical_today(cur, day: str) -> int:
    cur.execute(
        f"SELECT count(*) FROM {schema()}.notify_log "
        "WHERE sent_date = %s AND priority = 'critical' AND immediate = true",
        (day,),
    )
    return cur.fetchone()[0]


def _log(cur, source: str, priority: str, immediate: bool, text) -> None:
    cur.execute(
        f"INSERT INTO {schema()}.notify_log (sent_date, source, priority, immediate, text) "
        "VALUES (%s, %s, %s, %s, %s)",
        (timeutil.today(), source, priority, immediate, text),
    )


def notify(source: str, priority: str, text: str, *, parse_mode: str | None = None) -> dict:
    """Основная точка входа для подавляющего большинства инициаторов.
    parse_mode — прокидывается в Telegram ТОЛЬКО для немедленной отправки
    (red_flag/critical в бюджете); пункт, ушедший в дайджест, собирается
    в дайджест как обычный текст (свой parse_mode дайджест не поддерживает —
    один пункт с HTML-разметкой в комбинированном сообщении сломал бы вид
    остальных секций без него) — см. app/digest.py.
    Возвращает {"sent_immediately": bool, "ok": bool | None} — "ok" только
    когда реально пытались отправить сейчас (red_flag/critical в бюджете)."""
    if priority not in PRIORITIES:
        raise ValueError(f"notify: неизвестный приоритет {priority!r}, ожидается один из {PRIORITIES}")
    day = timeutil.today()
    with get_conn() as conn, conn.cursor() as cur:
        if priority == "red_flag":
            ok = _send(text, parse_mode)
            _log(cur, source, priority, True, text)
            conn.commit()
            return {"sent_immediately": True, "ok": ok}

        if priority == "critical":
            used = _count_immediate_critical_today(cur, day.isoformat())
            if used < CRITICAL_DAILY_BUDGET:
                ok = _send(text, parse_mode)
                _log(cur, source, priority, True, text)
                conn.commit()
                return {"sent_immediately": True, "ok": ok}
            logger.info("notify: дневной бюджет critical (%d) исчерпан — %s уходит в дайджест",
                        CRITICAL_DAILY_BUDGET, source)
            _log(cur, source, priority, False, text)
            conn.commit()
            return {"sent_immediately": False, "ok": None}

        # normal / digest — всегда в дайджест, разницы в обработке нет
        _log(cur, source, priority, False, text)
        conn.commit()
        return {"sent_immediately": False, "ok": None}


def log_external_send(source: str, priority: str = "red_flag") -> None:
    """Для отправителей со своей логикой доставки, которую нельзя заменить
    обычным notify() без потери надёжности — сейчас единственный случай:
    doctor/intake.py::_deliver_emergency (3 попытки ботом доктора + фолбэк
    через Hermes, F1-фикс 2026-09-22 — трогать нельзя ни на йоту). Они шлют
    сами, но обязаны залогировать сюда для журнала/бюджета. Текст НЕ
    сохраняем (эмердженси-содержание уже есть в card.rf_event/journal —
    незачем дублировать клиническую переписку во второй таблице)."""
    day = timeutil.today()
    with get_conn() as conn, conn.cursor() as cur:
        _log(cur, source, priority, True, None)
        conn.commit()
