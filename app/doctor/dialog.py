"""
card.dialog_turn — разговорная память (план §3.3): окно последних ходов внутри
сессии, идемпотентность по (chat_id, update_id). Тот же курсор-паттерн, что
app/write_path.py / app/journal.py — вызывающий передаёт активный курсор,
транзакцию открывает и коммитит сам.

card.dialog_turn — сущность, которой нет в спеке П1-П8 (честно отмечено в плане
§7.6): у source_message нет привязки к треду, а Simple Memory в n8n сегодня живёт
только в оперативной памяти воркфлоу и обнуляется на каждом рестарте — эта
таблица делает то же самое честно персистентным.
"""
import json
from typing import Optional

from psycopg import sql
from ulid import ULID

from app.db import schema
from app.doctor.config import SESSION_TTL_HOURS, SESSION_WINDOW_TURNS


def new_turn_id() -> str:
    return f"dt_{ULID()}"


def already_processed(cur, chat_id: str, update_id: Optional[int]) -> bool:
    """Идемпотентность по контракту Телеграма (переотправка апдейта). Уникальный
    индекс dialog_turn_update_idx физически не даст вставить дубль — эта проверка
    экономит вызов Telegram API/модели ДО того, как до индекса дойдёт дело, не
    заменяет его."""
    if update_id is None:
        return False
    cur.execute(
        sql.SQL("SELECT 1 FROM {t} WHERE chat_id = %s AND update_id = %s")
        .format(t=sql.Identifier(schema(), "dialog_turn")),
        (chat_id, update_id),
    )
    return cur.fetchone() is not None


def next_turn_index(cur, chat_id: str) -> int:
    cur.execute(
        sql.SQL("SELECT COALESCE(MAX(turn_index), -1) + 1 FROM {t} WHERE chat_id = %s")
        .format(t=sql.Identifier(schema(), "dialog_turn")),
        (chat_id,),
    )
    return cur.fetchone()[0]


def write_turn(cur, *, chat_id: str, role: str, text: str,
               update_id: Optional[int] = None, person_id: str = "self",
               turn_index: Optional[int] = None, meta: Optional[dict] = None,
               rf_level: Optional[str] = None, wrote_anything: bool = False) -> str:
    turn_id = new_turn_id()
    if turn_index is None:
        turn_index = next_turn_index(cur, chat_id)
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, person_id, chat_id, update_id, role, text, turn_index, "
            "meta, rf_level, wrote_anything) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        ).format(t=sql.Identifier(schema(), "dialog_turn")),
        (turn_id, person_id, chat_id, update_id, role, text, turn_index,
         json.dumps(meta or {}), rf_level, wrote_anything),
    )
    return turn_id


def recent_turns(cur, chat_id: str, limit: int = SESSION_WINDOW_TURNS,
                  ttl_hours: float = SESSION_TTL_HOURS) -> list[dict]:
    """Последние ходы ЖИВОЙ сессии (свежие ttl_hours), не весь диалог за всё время —
    хронологический порядок (старые первые), готово вставлять прямо в промпт.

    Сортировка по turn_index, не по ts: несколько ходов, записанных в одной
    транзакции, получают одинаковый now() (Postgres — время транзакции, не
    вызова) — turn_index строго возрастает per chat_id и не имеет этой
    неоднозначности."""
    cur.execute(
        sql.SQL(
            "SELECT id, role, text, ts, turn_index, meta, rf_level, wrote_anything "
            "FROM {t} WHERE chat_id = %s AND ts >= now() - (%s || ' hours')::interval "
            "ORDER BY turn_index DESC LIMIT %s"
        ).format(t=sql.Identifier(schema(), "dialog_turn")),
        (chat_id, ttl_hours, limit),
    )
    cols = ["id", "role", "text", "ts", "turn_index", "meta", "rf_level", "wrote_anything"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows.reverse()
    return rows
