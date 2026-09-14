"""
Журнал (П1 принцип 2 — "Current-state + журнал... полная археология"): каждое
создание/изменение объекта карты пишется сюда, в дополнение к самой записи.

Gap 1 из CARD_ARCHITECTURE_PLAN_2026-09-13.md §5 — таблица существовала со старта
(Phase 1), но ничего в неё не писало: комментарий "-> journal" в write_path.py
описывал желаемое, не реальное. Этот модуль закрывает разрыв.

Область действия (осознанно, не переусердствуем на первом проходе):
- `diff` фиксирует поля, УСТАНОВЛЕННЫЕ этой операцией (для create — весь снимок
  создания; для update/close — изменённые поля с новым значением). Полный
  до/после-дифф с отдельным SELECT перед каждым UPDATE НЕ делаем — цепочка
  журнала по одному object_id, прочитанная по порядку, и так восстанавливает
  историю; экономим на лишних запросах ради того, что не требуется инвариантом
  ("восстановим с нуля" = "восстановим из последовательности записей", не
  "каждая запись самодостаточна без контекста").
- Пишется В ТОЙ ЖЕ транзакции (тот же курсор), что и сама запись объекта —
  атомарность: либо обе строки, либо ни одной.
"""
import json
from typing import Optional

from psycopg import sql
from ulid import ULID

from app.db import schema

# Таблицы, у которых есть колонка journal_ref (обратная ссылка объект -> его
# журнальная запись создания). Не все объекты её имеют (expectation,
# recommendation_verdict — нет) — линкуем только там, где схема это поддерживает.
_HAS_JOURNAL_REF = {
    "fact", "episode", "problem", "intervention", "opinion",
    "disagreement", "recommendation", "visit", "lab_result", "memory_note",
}


def write_journal(
    cur,
    object_type: str,
    object_id: str,
    op: str,
    diff: Optional[dict] = None,
    actor: str = "system",
    reason: Optional[str] = None,
    link_back: bool = False,
) -> str:
    """Пишет одну строку в card.journal. Вызывать с курсором активной транзакции
    записи объекта — не открывает свою.

    link_back=True (только для op='create' и таблиц из _HAS_JOURNAL_REF) —
    дополнительно проставляет journal_ref у самого объекта на эту запись.
    """
    j_id = f"j_{ULID()}"
    table = sql.Identifier(schema(), "journal")
    cur.execute(
        sql.SQL(
            "INSERT INTO {table} (j_id, object_id, object_type, op, actor, diff, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ).format(table=table),
        (j_id, object_id, object_type, op, actor, json.dumps(diff) if diff is not None else None, reason),
    )

    if link_back and object_type in _HAS_JOURNAL_REF:
        obj_table = sql.Identifier(schema(), object_type)
        cur.execute(
            sql.SQL("UPDATE {table} SET journal_ref = %s WHERE id = %s").format(table=obj_table),
            (j_id, object_id),
        )

    return j_id
