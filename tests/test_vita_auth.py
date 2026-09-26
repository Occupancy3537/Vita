"""Vita v1 (2026-09-26) — app/vita_auth.py: httpOnly cookie-сессия по паролю,
без токена в URL. .env.test задаёт VITA_PASSWORD/VITA_SESSION_SECRET тестовыми
значениями (см. conftest.py — load_dotenv до импорта этого модуля)."""
import time

from fastapi import HTTPException
import pytest

from app import vita_auth as va


def test_check_password_correct():
    assert va.check_password("test-vita-password-not-prod") is True


def test_check_password_wrong():
    assert va.check_password("что-то другое") is False


def test_check_password_empty_input():
    assert va.check_password("") is False


def test_check_password_fails_closed_when_secret_missing(monkeypatch):
    monkeypatch.setattr(va, "_VITA_PASSWORD", "")
    assert va.check_password("любой-пароль") is False


def test_create_and_verify_session_token_round_trip():
    token = va.create_session_token()
    assert va.verify_session_token(token) is True


def test_verify_session_token_rejects_empty():
    assert va.verify_session_token("") is False


def test_verify_session_token_rejects_garbage():
    assert va.verify_session_token("не-похоже-на-токен") is False


def test_verify_session_token_rejects_expired():
    past = int(time.time()) - 3600
    token = va.create_session_token(now=past - va.SESSION_TTL_SECONDS)
    assert va.verify_session_token(token) is False


def test_verify_session_token_rejects_tampered_signature():
    token = va.create_session_token()
    expiry, sig = token.split(".", 1)
    tampered = f"{expiry}.{'0' * len(sig)}"
    assert va.verify_session_token(tampered) is False


def test_verify_session_token_rejects_tampered_expiry():
    """Подделка expiry без пересчёта подписи — HMAC не сойдётся (не «продлить
    себе сессию, просто написав дату побольше»)."""
    token = va.create_session_token()
    expiry, sig = token.split(".", 1)
    forged = f"{int(expiry) + 10**8}.{sig}"
    assert va.verify_session_token(forged) is False


def test_verify_session_token_fails_closed_when_secret_missing(monkeypatch):
    token = va.create_session_token()
    monkeypatch.setattr(va, "_VITA_SESSION_SECRET", "")
    assert va.verify_session_token(token) is False


class _FakeRequest:
    def __init__(self, cookies):
        self.cookies = cookies


def test_require_session_raises_401_without_cookie():
    with pytest.raises(HTTPException) as exc:
        va.require_session(_FakeRequest({}))
    assert exc.value.status_code == 401


def test_require_session_raises_401_with_invalid_cookie():
    with pytest.raises(HTTPException) as exc:
        va.require_session(_FakeRequest({va.COOKIE_NAME: "мусор"}))
    assert exc.value.status_code == 401


def test_require_session_passes_with_valid_cookie():
    token = va.create_session_token()
    va.require_session(_FakeRequest({va.COOKIE_NAME: token}))  # не бросает
