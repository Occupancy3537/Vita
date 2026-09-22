"""
LLM-извлечение (П2 §3.2) — structured output из свободного текста жалобы.
Слой A (regex, app/redflag.py) выполняется ДО этого шага и независимо — извлечение
не участвует в решении "неотложка или нет", оно только достаёт структуру симптома.

2026-09-22 (по запросу Влада, находка в логах OpenRouter): модель раньше была
отдельным литералом google/gemini-3.8-flash с комментарием "та же модель, что
уже используется в проекте" — комментарий устарел (доктор и Weekly Advisor
давно на GLM), а этот модуль вслед за ними не переехал, и стал самым дорогим
потребителем в логах. Теперь — общая точка выбора модели по роли, см.
app/ai_models.py (DEFAULT_MODEL — не DOCTOR_MODEL: обоснование в докстринге
ai_models.py, коротко — это дешёвая одноразовая классификация на каждое
сообщение, не дорогое рассуждение на ход диалога).
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

from app.ai_models import DEFAULT_MODEL

MODEL = DEFAULT_MODEL
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Версия промпта (тот же принцип версионирования, что у rv-engine/1, memory-render/1
# из спеки) — смена SYSTEM_PROMPT ниже требует бампа, чтобы card.extraction хранило,
# КАКИМ промптом извлечено, не только каким временем.
PROMPT_VERSION = "symptom-extract/1"

SYSTEM_PROMPT = """Ты извлекаешь структуру из жалобы пациента на здоровье, для медицинской карты.
НЕ ставь диагнозы, НЕ давай советы — только извлеки то, что сказано.

Верни JSON строго по схеме:
{
  "no_medical_content": bool,   // сообщение не про здоровье (например, "привет")
  "drafts": [
    {
      "symptom_key": string,    // короткий английский ключ, напр. "headache", "back_pain"
      "onset_expr": string,     // как пациент описал время, напр. "с утра"
      "intensity": int|null,    // 1-10 если указано
      "triggers": [string],     // что предшествовало/провоцирует
      "negation": bool,         // пациент говорит что симптома НЕТ / прошёл
      "closure": bool,          // явно закрывает тему ("прошло", "уже не болит")
      "confidence": float       // 0-1, насколько уверенно извлечение
    }
  ]
}
Если сообщение не про здоровье — no_medical_content=true, drafts=[].
Одно сообщение может содержать несколько симптомов — несколько drafts.
Только JSON, без пояснений."""


class Draft(BaseModel):
    symptom_key: str
    onset_expr: Optional[str] = None
    intensity: Optional[int] = None
    triggers: list[str] = []
    negation: bool = False
    closure: bool = False
    confidence: float = 0.5


class ExtractionResult(BaseModel):
    no_medical_content: bool = False
    drafts: list[Draft] = []
    raw_model_output: str = ""
    model: str = MODEL
    ts: datetime = datetime.now(timezone.utc)


def extract(text: str, timeout: float = 15.0) -> ExtractionResult:
    api_key = os.environ["OPENROUTER_API_KEY"]
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    drafts = [Draft(**d) for d in parsed.get("drafts", [])]
    return ExtractionResult(
        no_medical_content=parsed.get("no_medical_content", False),
        drafts=drafts,
        raw_model_output=content,
    )
