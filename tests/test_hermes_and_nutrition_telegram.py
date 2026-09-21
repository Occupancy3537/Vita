"""app/simple_telegram.py + app/hermes_telegram.py + app/nutrition_telegram.py
(2026-09-21, по прямому запросу Влада): алерты и отчёты о питании ушли не в
своих ботов, а в бота доктора — регрессия миграции с n8n (там алерты шли
через credential "Hermes Agent", отчёты о питании — через "Отчет по
питанию"). httpx мокается — тесты не бьют по Telegram API."""
import httpx
import pytest

from app import hermes_telegram, nutrition_telegram, simple_telegram


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


def test_hermes_telegram_raises_when_token_not_set(monkeypatch):
    monkeypatch.delenv("HERMES_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        hermes_telegram.send_message("8956401", "тест")


def test_hermes_telegram_uses_its_own_token(monkeypatch):
    monkeypatch.setenv("HERMES_BOT_TOKEN", "hermes-tok")
    captured = {}
    monkeypatch.setattr(simple_telegram, "send_message",
                         lambda token, chat_id, text, parse_mode=None: captured.update(token=token))
    hermes_telegram.send_message("8956401", "тест")
    assert captured["token"] == "hermes-tok"


def test_nutrition_telegram_raises_when_token_not_set(monkeypatch):
    monkeypatch.delenv("NUTRITION_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        nutrition_telegram.send_message("8956401", "тест")


def test_nutrition_telegram_uses_its_own_token(monkeypatch):
    monkeypatch.setenv("NUTRITION_BOT_TOKEN", "nutrition-tok")
    captured = {}
    monkeypatch.setattr(simple_telegram, "send_message",
                         lambda token, chat_id, text, parse_mode=None: captured.update(token=token))
    nutrition_telegram.send_message("8956401", "тест")
    assert captured["token"] == "nutrition-tok"
