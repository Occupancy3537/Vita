"""app/telegram_safe.py — живой инцидент 2026-09-23: httpx-исключение на
ошибке Telegram API несёт токен бота прямо в URL внутри своего текстового
представления, logger.exception() дальше по цепочке печатает его в docker
logs открытым текстом. redact()/raise_for_status_safe() — заплатка."""
import httpx
import pytest

from app.telegram_safe import raise_for_status_safe, redact


def test_redact_strips_token_from_url():
    text = "for url 'https://api.telegram.org/bot8413234787:AAEUX3sgJcD13_N8QTJMJkbBHmwbAXW-bP4/getUpdates?offset=1'"
    out = redact(text)
    assert "AAEUX3sgJcD13_N8QTJMJkbBHmwbAXW-bP4" not in out
    assert "/bot***" in out


def test_redact_leaves_normal_text_untouched():
    assert redact("Client error '409 Conflict' for url '...'") == "Client error '409 Conflict' for url '...'"


def test_raise_for_status_safe_redacts_token_in_error():
    request = httpx.Request("GET", "https://api.telegram.org/bot123456:REALSECRETTOKEN/getUpdates")
    response = httpx.Response(409, request=request, text='{"ok":false}')
    with pytest.raises(RuntimeError) as ei:
        raise_for_status_safe(response)
    assert "REALSECRETTOKEN" not in str(ei.value)
    assert "409" in str(ei.value)


def test_raise_for_status_safe_no_op_on_success():
    request = httpx.Request("GET", "https://api.telegram.org/bot123:x/getUpdates")
    response = httpx.Response(200, request=request, text="{}")
    raise_for_status_safe(response)  # не бросает
