"""Харнесс для проверки Vita в браузере: только роутер /vita без пароля, читает РЕАЛЬНУЮ БД (только чтение,
записи в БД из интерфейса не делать). Нужны CARD_PG_* как у card-service, VITA_PASSWORD/VITA_SESSION_SECRET любые."""
import mimetypes
import pathlib

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import vita
from app.vita_auth import require_session

ROOT = pathlib.Path(__file__).resolve().parents[2]
mimetypes.add_type("font/woff2", ".woff2")
app = FastAPI()
app.dependency_overrides[require_session] = lambda: None
app.include_router(vita.router)
app.mount("/vita-assets", StaticFiles(directory=str(ROOT / "app/static/vita_public")))


@app.get("/harness", response_class=HTMLResponse)
def harness():
    return (ROOT / "app/static/vita.html").read_text(encoding="utf-8")
