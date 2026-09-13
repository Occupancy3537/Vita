"""
LLM-извлечение (П2 §3.2) — structured output из свободного текста жалобы.
Слой A (regex, app/redflag.py) выполняется ДО этого шага и независимо — извлечение
не участвует в решении "неотложка или нет", оно только достаёт структуру симптома.

Модель — та же, что уже используется в проекте (Sub-Agent: AI Doctor, Weekly AI
Advisor) через OpenRouter — не выбирал новую без причины.
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

MODEL = "google/gemini-3.8-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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
