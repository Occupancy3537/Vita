"""«Детектив — жизненный цикл проблем» (2026-09-26). Одна забота: card.problem
create/close (путей не существовало вовсе — card.problem была одна миграционная
строка L5/S1, app/memory.py честно отмечал это как несделанное, §6.3) + правило
symptom_key -> активная problem для НОВЫХ эпизодов (Часть 2.1) + presumed_resolved
после 30 дней тишины (Часть 2.2). Направленный анализ гипотез — отдельный модуль
app/detective.py (другая забота: не создание/закрытие, а чтение и сопоставление).

Правило автопривязки (Часть 2.1: "неоднозначно — спросить Влада через доктора,
не угадывать") реализовано БЕЗ интерактивного канала внутри write_path.py (у
конвейера извлечения его физически нет — /ingest, а не диалог) — единственный
детерминированный, никогда не гадающий путь:
  - у symptom_key уже РОВНО ОДНА активная problem среди прежних эпизодов -> линкуем;
  - ни одной -> не линкуем (новый эпизод остаётся сиротой, пока человек/доктор
    не решит вопросом Create_Problem(symptom_keys=[...]), где связка explicit,
    а не угаданная);
  - больше одной (реальное противоречие в данных, не должно случаться при
    честной разметке) -> НЕ линкуем и оставляем видимый след в card.issue_log
    (тот же "не пропадает молча", что и у отклонённых G-воротами черновиков) —
    это и есть "спросить Влада", просто через уже существующий канал бэклога
    находок (issue_review.py, воскресный разбор), а не выдуманный новый.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg import sql
from ulid import ULID

from app import run_log, timeutil
from app.db import get_conn, schema
from app.journal import write_journal
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

PRESUMED_RESOLVED_SILENCE_DAYS = 30
MAINTENANCE_HOUR_VL = 9
MAINTENANCE_MINUTE_VL = 10  # до anomaly_detector daily (09:15) — не пересекается


def _problem_table():
    return sql.Identifier(schema(), "problem")


def _episode_table():
    return sql.Identifier(schema(), "episode")


def create_problem(cur, title: str, symptom_keys: Optional[list[str]] = None,
                    icd_hint: Optional[str] = None) -> dict:
    """Создаёт card.problem (status='active') и, если даны symptom_keys, СРАЗУ
    привязывает к ней существующие эпизоды с этими ключами, у которых ещё нет
    problem_id (Часть 2.1: doctor-инструмент — единственный способ ЯВНО
    объявить связку symptom_key -> problem, из которой потом растёт автолинк
    для будущих эпизодов). Уже привязанные к ДРУГОЙ проблеме эпизоды не
    перезаписываются молча — считаются и возвращаются отдельно."""
    problem_id = f"pb_{ULID()}"
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, ts_event, provenance, title, icd_hint, status, opened_ts) "
            "VALUES (%s, now(), %s, %s, %s, 'active', now())"
        ).format(t=_problem_table()),
        (problem_id, json.dumps({"origin": "doctor_chat"}), title, icd_hint),
    )
    write_journal(cur, "problem", problem_id, "create",
                  diff={"title": title, "icd_hint": icd_hint, "symptom_keys": symptom_keys or []},
                  link_back=True)

    linked, skipped = 0, 0
    if symptom_keys:
        for key in symptom_keys:
            cur.execute(
                sql.SQL("UPDATE {t} SET problem_id = %s WHERE symptom_key = %s AND problem_id IS NULL")
                .format(t=_episode_table()),
                (problem_id, key),
            )
            linked += cur.rowcount
            cur.execute(
                sql.SQL("SELECT count(*) FROM {t} WHERE symptom_key = %s AND problem_id IS NOT NULL AND problem_id != %s")
                .format(t=_episode_table()),
                (key, problem_id),
            )
            skipped += cur.fetchone()[0]
        if linked:
            write_journal(cur, "problem", problem_id, "update",
                          diff={"linked_episodes": linked, "symptom_keys": symptom_keys},
                          reason="Create_Problem: привязка существующих эпизодов")
    return {"id": problem_id, "linked_episodes": linked, "skipped_already_linked": skipped}


def close_problem(cur, problem_id: str, status: str, summary: str,
                   what_helped: Optional[str] = None) -> Optional[dict]:
    """Закрывает активную problem: status (resolved/chronic/obsolete) + closed_ts +
    case_summary jsonb (Часть 1.3: "что было, чем закончилось, что помогло").
    Открытые эпизоды этой проблемы получают итоговый статус (Часть 1.3:
    "эпизоды получают итоговый статус") — 'resolved', closure_source='problem_closed',
    независимо от терминального статуса самой проблемы (chronic тоже означает
    "эта КОНКРЕТНАЯ ветка разбора закрыта", не "симптомы продолжаются вечно
    под этим же эпизодом" — новый эпизод по тому же symptom_key откроет
    следующую главу, если понадобится). Возвращает None, если problem не
    найдена или уже не активна (вызывающий в commit.py решает, считать ли
    это ошибкой)."""
    case_summary = {"summary": summary, "what_helped": what_helped}
    cur.execute(
        sql.SQL(
            "UPDATE {t} SET status = %s, closed_ts = now(), case_summary = %s "
            "WHERE id = %s AND status = 'active' RETURNING title"
        ).format(t=_problem_table()),
        (status, json.dumps(case_summary), problem_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    title = row[0]

    cur.execute(
        sql.SQL(
            "UPDATE {t} SET status = 'resolved', end_ts = COALESCE(end_ts, now()), "
            "closure_source = 'problem_closed' WHERE problem_id = %s AND status = 'open'"
        ).format(t=_episode_table()),
        (problem_id,),
    )
    closed_episodes = cur.rowcount

    write_journal(cur, "problem", problem_id, "close",
                  diff={"status": status, "case_summary": case_summary, "closed_episodes": closed_episodes},
                  reason="Close_Problem (доктор)")

    from app.memory import create_case_summary_note
    create_case_summary_note(cur, problem_id, title, status, summary, what_helped)

    return {"id": problem_id, "title": title, "closed_episodes": closed_episodes}


def link_new_episode(cur, symptom_key: str, episode_id: str) -> Optional[str]:
    """Часть 2.1 — вызывается ТОЛЬКО для НОВОГО эпизода (write_path.py, ветка
    "создать", не "обновить существующий" — у уже открытого эпизода problem_id
    либо уже стоит, либо намеренно нет, трогать не нужно). Смотри докстринг
    модуля про то, почему "неоднозначно" здесь не значит "интерактивный вопрос
    прямо тут" — это детерминированный код без диалогового канала."""
    cur.execute(
        sql.SQL(
            "SELECT DISTINCT e.problem_id FROM {e} e JOIN {p} p ON p.id = e.problem_id "
            "WHERE e.symptom_key = %s AND p.status = 'active'"
        ).format(e=_episode_table(), p=_problem_table()),
        (symptom_key,),
    )
    candidates = [r[0] for r in cur.fetchall()]
    if len(candidates) == 1:
        cur.execute(
            sql.SQL("UPDATE {t} SET problem_id = %s WHERE id = %s").format(t=_episode_table()),
            (candidates[0], episode_id),
        )
        return candidates[0]
    if len(candidates) > 1:
        from app.issue_log import record_issue
        record_issue(
            cur, f"episode_problem_ambiguous:{symptom_key}", source="problem.link_new_episode",
            summary=f"symptom_key={symptom_key!r} привязан к {len(candidates)} разным активным problem "
                    f"({', '.join(candidates)}) — новый эпизод {episode_id} НЕ привязан автоматически, "
                    "нужно решение человека (см. Create_Problem/Close_Problem)",
        )
    return None


def run_daily_maintenance() -> int:
    """Часть 2.2 — эпизоды status='open' без активности (ни одного card.fact
    с этим episode_id) дольше PRESUMED_RESOLVED_SILENCE_DAYS дней -> отдельный
    статус 'presumed_resolved' (НЕ 'resolved' — та отметка означает явное
    "прошло" в диалоге, эта — предположение по тишине, две разные степени
    уверенности, путать нельзя). end_ts = момент последней активности (честная
    оценка "когда в последний раз было упомянуто", не выдуманное "закончилось
    именно сегодня")."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRESUMED_RESOLVED_SILENCE_DAYS)
    updated = 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT e.id, COALESCE(MAX(f.ts_event), e.onset_ts, e.ts_event) AS last_activity "
                "FROM {e} e LEFT JOIN {f} f ON f.episode_id = e.id "
                "WHERE e.status = 'open' "
                "GROUP BY e.id, e.onset_ts, e.ts_event "
                "HAVING COALESCE(MAX(f.ts_event), e.onset_ts, e.ts_event) < %s"
            ).format(e=_episode_table(), f=sql.Identifier(schema(), "fact")),
            (cutoff,),
        )
        rows = cur.fetchall()
        for ep_id, last_activity in rows:
            cur.execute(
                sql.SQL(
                    "UPDATE {t} SET status = 'presumed_resolved', end_ts = %s, closure_source = 'presumed_timeout' "
                    "WHERE id = %s"
                ).format(t=_episode_table()),
                (last_activity, ep_id),
            )
            write_journal(cur, "episode", ep_id, "close",
                          diff={"status": "presumed_resolved", "closure_source": "presumed_timeout"},
                          reason=f"тишина >{PRESUMED_RESOLVED_SILENCE_DAYS}д с {last_activity}")
            updated += 1
        conn.commit()
    if updated:
        logger.info("problem: %d эпизодов помечены presumed_resolved (тишина >%dд)", updated, PRESUMED_RESOLVED_SILENCE_DAYS)
    return updated


def run_scheduler() -> None:
    logger.info("problem maintenance scheduler: старт (%02d:%02d ВЛ)", MAINTENANCE_HOUR_VL, MAINTENANCE_MINUTE_VL)
    while True:
        try:
            timeutil.sleep_until_local(MAINTENANCE_HOUR_VL, MAINTENANCE_MINUTE_VL)
            run_daily_maintenance()
            run_log.mark_run("problem_maintenance")
        except Exception as e:
            logger.exception("problem maintenance scheduler упал — повтор завтра")
            alert_on_failure("problem_maintenance", e)
            time.sleep(24 * 3600)
