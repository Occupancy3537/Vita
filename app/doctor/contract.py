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
        "investigation_close", "lab_plan", "recommendation_close", "anomaly_dispose",
    ]
    payload: dict


class TurnResult(BaseModel):
    turn_id: str
    reply_text: str
    rf_level: Optional[Literal["L1", "L2", "L3"]] = None
    wrote_anything: bool = False
    staged_writes: list[StagedWrite] = Field(default_factory=list)


# --- аргументы write-инструментов (план §3.5) --------------------------------
# Валидируются здесь, ДО того как попасть в StagedWrite.payload — невалидный
# вызов возвращается модели как ошибка инструмента, не исполняется (план §3.4:
# "детерминированные ворота ДО исполнения инструмента... невалидный вызов не
# исполняется"). Бизнес-инварианты (одно открытое расследование и т.п.) — не
# здесь, это commit.py (Phase 5, §3.6: "инварианты переезжают из промпта в код",
# явно отнесены к записи, не к форме аргументов).

class RecordSymptomArgs(BaseModel):
    symptom_id: str = Field(description="Слаг латиницей, тот же при продолжении темы")
    symptom: str
    system: Optional[str] = None
    severity: Optional[int] = Field(None, ge=1, le=10)
    status: Literal["active", "monitoring", "resolved"] = "active"
    change: Optional[str] = None
    domain: Optional[str] = None
    context: Optional[str] = None
    hypothesis: Optional[str] = None
    notes: Optional[str] = None


class RecordNoteArgs(BaseModel):
    category: str
    note: str
    trigger: Optional[str] = None
    plan: Optional[str] = None


class OpenInvestigationArgs(BaseModel):
    inv_id: str = Field(description="Слаг латиницей")
    trigger: str
    trigger_detail: Optional[str] = None
    hypothesis: Optional[str] = None


class UpdateInvestigationArgs(BaseModel):
    inv_id: str
    findings: Optional[str] = None
    hypothesis: Optional[str] = None
    questions_pending: Optional[str] = None
    labs_suggested: Optional[str] = None


class CloseInvestigationArgs(BaseModel):
    inv_id: str
    findings: Optional[str] = None
    doctor_brief: Optional[str] = None
    referral: Optional[str] = None


class PlanLabArgs(BaseModel):
    test: str
    category: Optional[str] = None
    interval_months: Optional[int] = None
    reason: Optional[str] = None


class CloseRecommendationArgs(BaseModel):
    """«Петля исходов» (2026-09-24, часть 4) — единственное исключение из «не
    трогать app/doctor/» в этом тикете. title — подстрока названия, не id: у
    доктора нет способа знать внутренние rc_-идентификаторы (дайджест/дашборд их
    не показывают пациенту), поиск по названию в commit.py, отказ при 0 или >1
    совпадений вместо угадывания."""
    title: str = Field(description="Слово/фраза из названия рекомендации, например 'кардиолог'")
    reason: Optional[str] = None


class HypothesisItem(BaseModel):
    """Часть 2.2 тикета «мост аномалия -> действие»: у каждой гипотезы —
    "чем закрывается" (какое наблюдение различит её от альтернатив), не просто
    догадка без способа проверить."""
    hypothesis: str
    differentiator: str = Field(description="Какое наблюдение подтвердит/опровергнет именно эту гипотезу")


class DisposeAnomalyArgs(BaseModel):
    """«Мост аномалия -> действие» (2026-09-25, G5 VISION) — единственное
    исключение из «не трогать app/doctor/» в этом тикете (вместе с блоком
    истории диспозиций в context.py — контекст для генерации гипотез,
    не отдельная интеграция). metric — подстрока (та же схема резолва, что
    CloseRecommendationArgs.title), не внутренний id."""
    metric: str = Field(description="Слово/фраза из названия метрики в алерте, например 'ВСР' или 'сон'")
    disposition: Literal["investigate", "suppress", "acknowledge"]
    reason: Optional[str] = None
    window_days: Optional[int] = Field(None, description="Только для suppress — окно тишины в днях, по умолчанию 30")
    hypotheses: list[HypothesisItem] = Field(
        default_factory=list,
        description="Только для investigate — 1-3 гипотезы по контексту дня аномалии, у каждой differentiator",
    )
