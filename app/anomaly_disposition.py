"""Мост «аномалия → действие» (2026-09-25, G5 VISION: «каждая аномалия ведёт
к гипотезе или действию, а не просто к сообщению "странно"»). Раньше цепочка
обрывалась на notify-алерте — объекта не рождалось. Последнее недостающее
звено главной петли: аномалия → гипотеза → действие с ожиданием → вердикт
(первые три уже построены — детектор, health.investigations, card.recommendation
с ожиданиями через G7).

card.anomaly_disposition — операционное состояние (как notify_log/
err_dedup_state), не полная card-объектная модель: это workflow-статус
аномалии (кто решил что с ней делать), не отдельный клинический факт.

Диспозиции: pending (по умолчанию) / investigate / suppress / acknowledge.
"explained" — не диспозиция, которую назначает Влад, а терминальный подстатус
investigate, который система выставляет САМА, когда связанное расследование
закрывается (см. sync_resolved_investigations) — Часть 4.1 тикета.

app/doctor/ ЗДЕСЬ НЕ ИМПОРТИРУЕТСЯ И НЕ ИМПОРТИРУЕТ ЭТОТ МОДУЛЬ НАПРЯМУЮ в
обход инструмента — Dispose_Anomaly (app/doctor/tools.py) вызывает dispose()
отсюда, это и есть "новый инструмент доктору", не более широкая интеграция.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from psycopg import sql
from ulid import ULID

from app.db import schema
from app.journal import write_journal

logger = logging.getLogger(__name__)

# 2026-09-21/#38/#47: тот же переключатель, что app/registrar.py и
# app/doctor/commit.py — тесты изолируют health.investigations от прода.
_HEALTH_SCHEMA = os.environ.get("REGISTRAR_HEALTH_SCHEMA", "health")

DISPOSITIONS = ("pending", "investigate", "suppress", "acknowledge")
SETTABLE_DISPOSITIONS = ("investigate", "suppress", "acknowledge")  # pending — только начальное, не назначается руками
# "explained" — терминальный подстатус investigate, выставляется СИСТЕМОЙ
# (sync_resolved_investigations), не Владом напрямую — см. докстринг ниже.
ALL_DISPOSITION_VALUES = DISPOSITIONS + ("explained",)
SUPPRESS_DEFAULT_DAYS = 30
SERIES_WINDOW_DAYS = 7
SERIES_MIN_COUNT = 3

REPLY_HINT = "ответь: разбираемся / известно / следи"
# Естественные слова Влада -> disposition (Часть 1.3 тикета, дословно из текста
# алерта) — доктор сам понимает свободную речь через LLM, это просто канонический
# словарь для документации/тестов, не парсер (парсинга реплаев нет, см. докстринг).
REPLY_WORD_TO_DISPOSITION = {"разбираемся": "investigate", "известно": "acknowledge", "следи": "suppress"}


def _t():
    return sql.Identifier(schema(), "anomaly_disposition")


# =====================================================================
# Часть 1.2 — серия: 3 moderate по одной метрике за 7 дней = эскалация
# =====================================================================

def check_series_escalation(cur, metric_key: str, day: str, window_days: int = SERIES_WINDOW_DAYS,
                             min_count: int = SERIES_MIN_COUNT) -> int:
    """Считает moderate-вхождения metric_key за [day-window_days+1, day]
    (включительно) по health.anomaly_log.raw_anomalies — ЧИТАЕТ то, что детектор
    уже записал, не трогает сам детектор/z-score (граница тикета). Возвращает
    счётчик — вызывающий сравнивает с min_count сам.

    Литеральный "health.anomaly_log", НЕ _HEALTH_SCHEMA: эта таблица не входит
    в набор с health_test-двойником (tests/conftest.py::HEALTH_TEST_TABLES_TO_CLEAN
    её не перечисляет — тот же приём, что уже использует сам app/anomaly_detector.py
    для этой же таблицы), изоляция тестов — через _isolate_real_schema_writes
    (neutered commit), не через подмену имени схемы."""
    frm = (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=window_days - 1)).isoformat()
    cur.execute(
        "SELECT raw_anomalies FROM health.anomaly_log WHERE date >= %s AND date <= %s",
        (frm, day),
    )
    count = 0
    for (raw,) in cur.fetchall():
        anomalies = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        for a in anomalies:
            if a.get("metric") == metric_key and a.get("severity") == "moderate":
                count += 1
    return count


# =====================================================================
# Часть 3.1 — suppress: активное подавление на метрику
# =====================================================================

def active_suppression(cur, metric_key: str, day: str) -> Optional[dict]:
    """Действующее подавление (suppress_until >= day) на эту метрику, если
    есть — вызывающий (anomaly_detector) не будит critical, но всё равно пишет
    строку диспозиции (Часть 3.1: "видимая тишина, не слепота")."""
    cur.execute(
        sql.SQL("SELECT id, reason, suppress_until FROM {t} WHERE metric_key = %s "
                "AND disposition = 'suppress' AND suppress_until >= %s "
                "ORDER BY suppress_until DESC LIMIT 1").format(t=_t()),
        (metric_key, day),
    )
    row = cur.fetchone()
    return {"id": row[0], "reason": row[1], "suppress_until": row[2]} if row else None


# =====================================================================
# Создание диспозиции (вызывает anomaly_detector.py при strong/эскалации)
# =====================================================================

def create_disposition_row(cur, metric_key: str, metric_label: Optional[str], day: str, severity: str,
                            disposition: str = "pending", reason: Optional[str] = None,
                            disposed_by: Optional[str] = None) -> tuple[str, bool]:
    """Идемпотентно по (metric_key, date) — ON CONFLICT DO NOTHING, повторный
    вызов на тот же день (например, если run_daily_check() дёрнули дважды)
    не плодит вторую строку. Возвращает (id, created)."""
    new_id = f"ad_{ULID()}"
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, metric_key, metric_label, date, severity, disposition, reason, "
            "disposed_ts, disposed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (metric_key, date) DO NOTHING RETURNING id"
        ).format(t=_t()),
        (new_id, metric_key, metric_label, day, severity, disposition, reason,
         datetime.now().astimezone() if disposition != "pending" else None, disposed_by),
    )
    row = cur.fetchone()
    if row:
        write_journal(cur, "anomaly_disposition", row[0], "create",
                      diff={"metric_key": metric_key, "date": day, "severity": severity, "disposition": disposition})
        return row[0], True
    cur.execute(
        sql.SQL("SELECT id FROM {t} WHERE metric_key = %s AND date = %s").format(t=_t()),
        (metric_key, day),
    )
    return cur.fetchone()[0], False


# =====================================================================
# Часть 1.4 — дайджест: pending старше суток, одной строкой
# =====================================================================

def pending_older_than(cur, hours: int = 24) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT metric_key, metric_label, date, severity FROM {t} "
                "WHERE disposition = 'pending' AND created_ts < now() - (%s || ' hours')::interval "
                "ORDER BY date").format(t=_t()),
        (hours,),
    )
    return [{"metric_key": k, "metric_label": l or k, "date": str(d), "severity": s} for k, l, d, s in cur.fetchall()]


def format_pending_digest_line(pending: list[dict]) -> Optional[str]:
    if not pending:
        return None
    items = "; ".join(f"{p['metric_label']} ({p['date']})" for p in pending)
    return f"⏳ Без решения больше суток: {items}. {REPLY_HINT}."


# =====================================================================
# Часть 1.3/2 — dispose(): назначение диспозиции доктором (Dispose_Anomaly)
# =====================================================================

def find_target_rows(cur, metric: str) -> list[str]:
    """Резолвит свободный "metric" (label или key, подстрока — тот же приём,
    что Close_Recommendation в петле исходов) в id строк диспозиции. Сначала
    pending (обычный случай — свежий алерт), если пусто — самая свежая по дате
    запись любой диспозиции (Влад передумал про уже решённую метрику)."""
    like = f"%{metric}%"
    cur.execute(
        sql.SQL("SELECT id FROM {t} WHERE (metric_key ILIKE %s OR metric_label ILIKE %s) "
                "AND disposition = 'pending'").format(t=_t()),
        (like, like),
    )
    rows = [r[0] for r in cur.fetchall()]
    if rows:
        return rows
    cur.execute(
        sql.SQL("SELECT id FROM {t} WHERE (metric_key ILIKE %s OR metric_label ILIKE %s) "
                "ORDER BY date DESC LIMIT 1").format(t=_t()),
        (like, like),
    )
    row = cur.fetchone()
    return [row[0]] if row else []


def _has_open_investigation(cur) -> bool:
    cur.execute(f"SELECT 1 FROM {_HEALTH_SCHEMA}.investigations WHERE lower(status) = 'open' LIMIT 1")
    return cur.fetchone() is not None


def _open_investigation_for_anomaly(cur, metric_key: str, reason: Optional[str],
                                     hypotheses: Optional[list[dict]]) -> Optional[str]:
    """Часть 2.1: "существующим механизмом investigations". НЕ переиспользует
    app/doctor/commit.py::_open_investigation напрямую — та бросает CommitError
    (откатывает ВЕСЬ StagedWrite-батч хода), здесь нужна честная деградация:
    диспозиция всё равно применяется, расследование — best effort. Если уже
    открыто — гипотезы остаются на строке диспозиции (Часть 2.4: "накопится
    база"), просто без отдельного inv_id."""
    if _has_open_investigation(cur):
        logger.info("anomaly_disposition: investigate для %r — уже есть открытое расследование, "
                    "гипотезы сохранены при аномалии, отдельное расследование не открыто", metric_key)
        return None
    inv_id = f"anomaly-{metric_key}-{str(ULID()).lower()}"[:64]
    hyp_text = "; ".join(
        f"{h.get('hypothesis')} (закрывается: {h.get('differentiator')})" for h in (hypotheses or []) if h.get("hypothesis")
    ) or None
    cur.execute(
        f"INSERT INTO {_HEALTH_SCHEMA}.investigations "
        "(inv_id, opened, updated, status, trigger, trigger_detail, hypothesis) "
        "VALUES (%s, CURRENT_DATE, CURRENT_DATE, 'open', %s, %s, %s)",
        (inv_id, f"anomaly:{metric_key}", reason or "аномалия метрики, диспозиция investigate", hyp_text),
    )
    return inv_id


def dispose(cur, metric: str, disposition: str, reason: Optional[str] = None,
            window_days: Optional[int] = None, hypotheses: Optional[list[dict]] = None,
            today: Optional[date] = None) -> dict:
    """Основная функция инструмента Dispose_Anomaly (вызывается из
    app/doctor/commit.py, единственная интеграция с app/doctor/, помимо
    генерации гипотез по досье — граница тикета)."""
    if disposition not in SETTABLE_DISPOSITIONS:
        return {"ok": False, "error": f"disposition должен быть один из {SETTABLE_DISPOSITIONS}"}

    ids = find_target_rows(cur, metric)
    if not ids:
        return {"ok": False, "error": f"не найдено аномалий по метрике {metric!r}"}

    suppress_until = None
    if disposition == "suppress":
        suppress_until = ((today or date.today()) + timedelta(days=window_days or SUPPRESS_DEFAULT_DAYS)).isoformat()

    cur.execute(
        sql.SQL("UPDATE {t} SET disposition = %s, reason = %s, suppress_until = %s, hypotheses = %s, "
                "disposed_ts = now(), disposed_by = 'vlad' WHERE id = ANY(%s)").format(t=_t()),
        (disposition, reason, suppress_until, json.dumps(hypotheses, ensure_ascii=False) if hypotheses else None, ids),
    )
    for row_id in ids:
        write_journal(cur, "anomaly_disposition", row_id, "update",
                      diff={"disposition": disposition, "reason": reason, "suppress_until": suppress_until})

    investigation_id = None
    if disposition == "investigate":
        investigation_id = _open_investigation_for_anomaly(cur, metric, reason, hypotheses)
        if investigation_id:
            cur.execute(sql.SQL("UPDATE {t} SET investigation_id = %s WHERE id = ANY(%s)").format(t=_t()),
                        (investigation_id, ids))

    return {"ok": True, "disposition": disposition, "count": len(ids), "investigation_id": investigation_id}


# =====================================================================
# Часть 4.1 — замыкание: расследование резолвится -> аномалия "explained"
# =====================================================================

def sync_resolved_investigations(cur) -> int:
    """Вызывается раз в сутки из anomaly_detector.run_daily_scheduler() (НЕ из
    самого детектора — после него, граница тикета). Не трогает
    app/doctor/commit.py::_close_investigation — просто читает итог её работы:
    investigation_id, чьё health.investigations.status уже не 'open' (значит
    расследование закрыто/готово), -> disposition='explained'."""
    cur.execute(
        sql.SQL("SELECT ad.id FROM {t} ad JOIN {inv} i ON i.inv_id = ad.investigation_id "
                "WHERE ad.disposition = 'investigate' AND lower(i.status) != 'open'")
        .format(t=_t(), inv=sql.Identifier(_HEALTH_SCHEMA, "investigations")),
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return 0
    cur.execute(
        sql.SQL("UPDATE {t} SET disposition = 'explained' WHERE id = ANY(%s)").format(t=_t()),
        (ids,),
    )
    for row_id in ids:
        write_journal(cur, "anomaly_disposition", row_id, "update", diff={"disposition": "explained"},
                      reason="расследование закрыто")
    return len(ids)


# =====================================================================
# Часть 3.3 / досье доктора — история диспозиций ("что я уже говорил про HRV")
# =====================================================================

def recent_dispositions(cur, limit: int = 10) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT metric_label, metric_key, date, severity, disposition, reason FROM {t} "
                "WHERE disposition != 'pending' ORDER BY disposed_ts DESC LIMIT %s").format(t=_t()),
        (limit,),
    )
    return [
        {"metric": label or key, "date": str(d), "severity": sev, "disposition": disp, "reason": reason}
        for label, key, d, sev, disp, reason in cur.fetchall()
    ]


# =====================================================================
# Часть 4.2 — недельный отчёт: "аномалии недели с судьбами"
# =====================================================================

def weekly_fates_summary(cur, since_date: str) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT metric_label, metric_key, date, severity, disposition FROM {t} "
                "WHERE date >= %s ORDER BY date").format(t=_t()),
        (since_date,),
    )
    return [
        {"metric": label or key, "date": str(d), "severity": sev, "disposition": disp}
        for label, key, d, sev, disp in cur.fetchall()
    ]
