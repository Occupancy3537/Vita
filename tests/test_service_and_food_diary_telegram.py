"""app/simple_telegram.py + app/service_telegram.py + app/food_diary_telegram.py
(2026-09-24, тикет «раскладка ботов по тематическим чатам»): было
test_hermes_and_nutrition_telegram.py — Hermes исключён из проекта, nutrition_telegram.py
репурпose-нут в service_telegram.py (тот же токен NUTRITION_BOT_TOKEN, новая
роль — сервисный бот app/notify.py). food_diary_telegram.py — новый минимальный
отправитель для сводки питания (FOOD_DIARY_BOT_TOKEN, тот же бот, что дневник
питания). httpx мокается — тесты не бьют по Telegram API."""
import httpx
import pytest

from app import food_diary_telegram, service_telegram, simple_telegram


def _resp(status_code=200, json_body=None):
    return httpx.Response(request=httpx.Request("POST", "http://test/"), status_code=status_code,
                           json=json_body if json_body is not None else {"ok": True, "result": {}})


def test_simple_telegram_send_message_posts_to_given_token(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    simple_telegram.send_message("TOK123", "8956401", "привет", parse_mode="HTML")

    assert captured["url"] == "https://api.telegram.org/botTOK123/sendMessage"
    assert captured["json"] == {"chat_id": "8956401", "text": "привет", "parse_mode": "HTML"}


def test_simple_telegram_omits_parse_mode_when_not_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: (captured.update(json=json), _resp())[1])
    simple_telegram.send_message("TOK", "1", "текст")
    assert "parse_mode" not in captured["json"]


def test_simple_telegram_raises_on_api_not_ok(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _resp(json_body={"ok": False, "description": "boom"}))
    with pytest.raises(RuntimeError):
        simple_telegram.send_message("TOK", "1", "текст")


def test_service_telegram_raises_when_token_not_set(monkeypatch):
    monkeypatch.delenv("NUTRITION_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        service_telegram.send_message("8956401", "тест")


def test_service_telegram_uses_its_own_token(monkeypatch):
    monkeypatch.setenv("NUTRITION_BOT_TOKEN", "service-tok")
    captured = {}
    monkeypatch.setattr(simple_telegram, "send_message",
                         lambda token, chat_id, text, parse_mode=None: captured.update(token=token))
    service_telegram.send_message("8956401", "тест")
    assert captured["token"] == "service-tok"


def test_food_diary_telegram_raises_when_token_not_set(monkeypatch):
    monkeypatch.delenv("FOOD_DIARY_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        food_diary_telegram.send_message("8956401", "тест")


def test_food_diary_telegram_uses_its_own_token(monkeypatch):
    monkeypatch.setenv("FOOD_DIARY_BOT_TOKEN", "food-diary-tok")
    captured = {}
    monkeypatch.setattr(simple_telegram, "send_message",
                         lambda token, chat_id, text, parse_mode=None: captured.update(token=token))
    food_diary_telegram.send_message("8956401", "тест")
    assert captured["token"] == "food-diary-tok"
