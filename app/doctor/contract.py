"""
Pydantic-контракты нового доктора (план §3.9). IncomingMessage — нормализованный
вход после intake.py, одинаковый независимо от транспорта (сегодня — временный
HTTP-хоп из n8n; шаг 2 плана §3.2 — long-polling или собственный вебхук card-service,
без изменений здесь).
"""
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

MessageKind = Literal["text", "photo", "voice", "document", "unknown"]


class IncomingMessage(BaseModel):
    chat_id: str
    update_id: int
    message_id: Optional[int] = None
    person_id: str = "self"
    text: Optional[str] = None
    kind: MessageKind = "text"
    photo_file_ids: list[str] = Field(default_factory=list)
    voice_file_id: Optional[str] = None
    document_file_id: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    reply_to_text: Optional[str] = None
    # #SYM:<id> — тег, которым Watchdog и доктор помечают свои сообщения о конкретном
    # симптоме (STATE.md, механизм анамнеза #A01). Реплай на такое сообщение — явное
    # продолжение той же темы, не новый эпизод.
    reply_symptom_id: Optional[str] = None
    forward_from: Optional[str] = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_update: dict = Field(default_factory=dict)


class ToolCall(BaseModel):
    name: str
    arguments: dict


class StagedWrite(BaseModel):
    """Промежуточный результат агентного цикла (§3.5) — то, что модель попросила
    записать через write-инструмент, ДО прохождения через commit.py (валидация +
    детерминированные инварианты, §3.6). commit.py решает, применять или нет."""
    kind: Literal[
        "symptom", "note", "investigation_open", "investigation_update",
        "investigation_close", "lab_plan",
    ]
    payload: dict


class TurnResult(BaseModel):
    turn_id: str
    reply_text: str
    rf_level: Optional[Literal["L1", "L2", "L3"]] = None
    wrote_anything: bool = False
    staged_writes: list[StagedWrite] = Field(default_factory=list)
