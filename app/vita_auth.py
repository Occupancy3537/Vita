"""Vita v1 (2026-09-26) — доступ по паролю + httpOnly cookie-сессия, НЕ токен
в URL (урок проекта: /dashboard/* годами носит токен в query-строке — он
попадает в историю браузера, логи nginx, шэринг ссылок; ПЛАН СБОРКИ макета
Vita прямым текстом требует не повторять это). Никакой БД для сессий не
заводилось: подписанный самодостаточный токен (expiry + HMAC) в cookie —
сервер ничего не хранит, проверка — чистая функция.

Секрет подписи (VITA_SESSION_SECRET) отдельный от пароля входа (VITA_PASSWORD)
намеренно: подделка cookie требует знания секрета подписи, а не пароля,
который человек вводит на клавиатуре и который короче/памятнее.
"""
import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import HTTPException, Request

COOKIE_NAME = "vita_session"
SESSION_TTL_SECONDS = 90 * 24 * 3600  # 90 дней — это карманное PWA на одном телефоне, не веб-сессия с чужого устройства

_VITA_PASSWORD = os.environ.get("VITA_PASSWORD", "")
_VITA_SESSION_SECRET = os.environ.get("VITA_SESSION_SECRET", "")


def _sign(expiry: int) -> str:
    msg = f"vita:{expiry}".encode()
    return hmac.new(_VITA_SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def create_session_token(now: Optional[int] = None) -> str:
    expiry = (now or int(time.time())) + SESSION_TTL_SECONDS
    return f"{expiry}.{_sign(expiry)}"


def verify_session_token(token: str) -> bool:
    """Fail-closed: пустой VITA_SESSION_SECRET (не задан в env) -> подпись
    никогда не совпадёт (тот же принцип, что DASHBOARD_TOKEN в main.py —
    пустой секрет не значит «пускать всех», значит «не пускать никого»)."""
    if not token or not _VITA_SESSION_SECRET:
        return False
    try:
        expiry_s, sig = token.split(".", 1)
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return False
    if expiry < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(expiry))


def check_password(password: str) -> bool:
    if not _VITA_PASSWORD:
        return False
    # compare_digest падает на не-ASCII str (нужны bytes или ASCII-only str) —
    # пароль вводится с телефонной клавиатуры, там реально может быть кириллица.
    return hmac.compare_digest((password or "").encode(), _VITA_PASSWORD.encode())


def require_session(request: Request) -> None:
    """FastAPI-зависимость: без валидной cookie — 401, и на саму страницу
    vita.html, и на её API (пункт 2 Части 1 тикета «Vita v1»)."""
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_session_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")
