"""Единственный тест с настоящим вызовом OpenRouter (не мок) — проверяет, что
интеграция реально работает, а не только что код синтаксически верен. Остальные
тесты (test_write_path.py) мокают extract() ради скорости/детерминизма — здесь
наоборот, детерминизм не важен, важно что модель отвечает валидным JSON по схеме."""
from app.extraction import extract


def test_real_extraction_call_returns_valid_structure():
    result = extract("голова болит с самого утра, тупая боль")
    assert result.no_medical_content is False
    assert len(result.drafts) >= 1
    assert result.drafts[0].symptom_key  # непустая строка


def test_real_extraction_no_medical_content():
    result = extract("привет, как дела?")
    assert result.no_medical_content is True
