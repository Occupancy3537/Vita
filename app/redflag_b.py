"""
П5 §4 — слой B: LLM-классификатор красных флагов, контекстно-свободный (§1.3 —
карта пациента НЕ передаётся, только текст + 2 предыдущие реплики). Распознаёт
признаки, не принимает решений — уровень эскалации вычисляет детерминированная
таблица §2.3 (app/redflag_union.py), не эта модель (F5).
"""
import json
import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from app.redflag import CRITICAL_CATEGORIES, HIGH_CATEGORIES

PROMPT_VERSION = "rf-classifier/1"
MODEL = "google/gemini-3.8-flash"  # та же модель, что extraction/advisor — не выбирал новую без причины
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

CATEGORIES = [
    "cardiac_acute", "neuro_acute", "anaphylaxis", "psych_crisis", "bleeding_gi",
    "sepsis_suspect", "severe_pain", "metabolic_acute", "systemic_warning", "none",
]

SYSTEM_PROMPT = """Ты — детектор признаков неотложных состояний в сообщении пациента, НЕ консультант.
Не ставь диагноз, не интерпретируй, не советуй. Только распознай, есть ли в тексте паттерн одной из категорий.

Категории:
- cardiac_acute: давящая боль за грудиной, одышка в покое, обморок, отёки+одышка
- neuro_acute: внезапная "худшая в жизни" головная боль, асимметрия лица, внезапная слабость конечности/нарушение речи/зрения
- anaphylaxis: отёк губ/языка/горла, генерализованная сыпь+затруднение дыхания, свист после препарата/пищи
- psych_crisis: суицидальные высказывания, план, прощальные формулировки
- bleeding_gi: рвота кровью/"кофейной гущей", чёрный стул
- sepsis_suspect: лихорадка+спутанность, лихорадка+ригидность шеи/сыпь
- severe_pain: внезапная боль 10/10 любой локализации, внезапная слепота
- metabolic_acute: "трясёт, льёт пот, сахар падает" — гипогликемический паттерн
- systemic_warning: неспецифичное "плохо себя чувствую" без чёткой категории выше
- none: ничего из перечисленного

Верни JSON строго по схеме:
{
  "hit": bool,
  "category": "одна из категорий выше или none",
  "modality": {"current": bool, "past": bool, "negation": bool, "hypothetical": bool, "third_party": bool},
  "severity_factors": {"duration_min": int|null, "intensity": "low"|"medium"|"high"|null, "combination": [string], "progression": string|null},
  "context_note": "1 фраза, что именно увидел",
  "confidence": float 0-1
}
hit=false -> category="none", modality всё false, остальное пусто. Только JSON, без пояснений."""


class Modality(BaseModel):
    current: bool = False
    past: bool = False
    negation: bool = False
    hypothetical: bool = False
    third_party: bool = False


class SeverityFactors(BaseModel):
    duration_min: Optional[int] = None
    intensity: Optional[str] = None
    combination: list[str] = Field(default_factory=list)
    progression: Optional[str] = None


class LayerBResult(BaseModel):
    hit: bool = False
    category: str = "none"
    modality: Modality = Field(default_factory=Modality)
    severity_factors: SeverityFactors = Field(default_factory=SeverityFactors)
    context_note: str = ""
    confidence: float = 0.0
    degraded: bool = False  # §4.3/§9: провал вызова/валидации -> degraded-кандидат, не "не флаг"


def _validate(parsed: dict) -> LayerBResult:
    """§4.3: категория ∈ enum; confidence ∈ [0,1]; hit=false -> остальное пусто."""
    category = parsed.get("category") if parsed.get("category") in CATEGORIES else "none"
    hit = bool(parsed.get("hit")) and category != "none"
    if not hit:
        return LayerBResult(hit=False, category="none")
    confidence = parsed.get("confidence")
    confidence = max(0.0, min(1.0, float(confidence))) if isinstance(confidence, (int, float)) else 0.5
    modality = Modality(**{k: bool(v) for k, v in (parsed.get("modality") or {}).items() if k in Modality.model_fields})
    sf_raw = parsed.get("severity_factors") or {}
    severity = SeverityFactors(
        duration_min=sf_raw.get("duration_min") if isinstance(sf_raw.get("duration_min"), int) else None,
        intensity=sf_raw.get("intensity") if sf_raw.get("intensity") in ("low", "medium", "high") else None,
        combination=[str(x) for x in sf_raw.get("combination", [])] if isinstance(sf_raw.get("combination"), list) else [],
        progression=sf_raw.get("progression") if isinstance(sf_raw.get("progression"), str) else None,
    )
    return LayerBResult(
        hit=True, category=category, modality=modality, severity_factors=severity,
        context_note=str(parsed.get("context_note", ""))[:300], confidence=confidence,
    )


def classify(text: str, prior_replies: Optional[list[str]] = None, timeout: float = 8.0) -> LayerBResult:
    """§1.3: без карты пациента — только текст + до 2 предыдущих реплик (анафора)."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return LayerBResult(degraded=True)

    context_msgs = (prior_replies or [])[-2:]
    user_content = "\n".join(context_msgs + [text]) if context_msgs else text

    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
        return _validate(parsed)
    except Exception:
        # §4.3: провал -> degraded-кандидат (очередь пост-обработки), НЕ "не флаг".
        # Полная очередь §9 не реализована в этом заходе — возвращаем degraded=True,
        # вызывающий обязан не читать это как отрицательный результат.
        return LayerBResult(degraded=True)
