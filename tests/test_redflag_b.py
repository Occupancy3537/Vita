"""П5 §4.3 — детерминированная валидация выхода слоя B: категория ∈ enum,
confidence ∈ [0,1], hit=false -> остальное пусто, провал вызова -> degraded,
НЕ "не флаг" (это не одно и то же)."""
import json
from unittest.mock import MagicMock, patch

import httpx

from app.redflag_b import classify


def _mock_llm(content: dict):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"choices": [{"message": {"content": json.dumps(content)}}]}
    return resp


def test_valid_hit_parsed_correctly():
    with patch("app.redflag_b.httpx.post", return_value=_mock_llm({
        "hit": True, "category": "neuro_acute",
        "modality": {"current": True, "past": False, "negation": False, "hypothetical": False, "third_party": False},
        "severity_factors": {"duration_min": None, "intensity": "high", "combination": ["слабость в руке"], "progression": None},
        "context_note": "внезапная слабость в руке", "confidence": 0.9,
    })):
        r = classify("резко onemela рука и не могу говорить")
    assert r.hit is True and r.category == "neuro_acute" and r.confidence == 0.9
    assert r.modality.current is True
    assert r.degraded is False


def test_hit_false_clears_other_fields_even_if_model_sent_garbage():
    with patch("app.redflag_b.httpx.post", return_value=_mock_llm({
        "hit": False, "category": "cardiac_acute",  # модель непоследовательна — валидация должна поправить
        "confidence": 0.5,
    })):
        r = classify("привет, как дела")
    assert r.hit is False and r.category == "none"


def test_unknown_category_degrades_to_none_not_crash():
    with patch("app.redflag_b.httpx.post", return_value=_mock_llm({
        "hit": True, "category": "not_a_real_category", "confidence": 0.5,
    })):
        r = classify("что-то странное")
    assert r.category == "none" and r.hit is False


def test_confidence_clamped_to_0_1():
    with patch("app.redflag_b.httpx.post", return_value=_mock_llm({
        "hit": True, "category": "severe_pain", "confidence": 5.0,
        "modality": {"current": True}, "severity_factors": {},
    })):
        r = classify("боль 10 из 10")
    assert r.confidence == 1.0


def test_network_failure_returns_degraded_not_no_hit():
    """§4.3/§9: провал вызова -> degraded-кандидат, НЕ эквивалент 'флага нет' —
    вызывающий должен уметь отличить эти два состояния."""
    with patch("app.redflag_b.httpx.post", side_effect=httpx.ConnectError("boom")):
        r = classify("что угодно")
    assert r.degraded is True
    assert r.hit is False  # но и не "флаг найден" — честно неизвестно


def test_no_api_key_returns_degraded():
    with patch.dict("os.environ", {}, clear=True):
        r = classify("текст")
    assert r.degraded is True
