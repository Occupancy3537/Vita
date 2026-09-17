"""Step 2 плана нового доктора (§3.2) — poller.py: смещение оффсета,
маршрутизация "доктор -> напрямую / иначе -> переслать Capitan", релей файлов
как multipart. Telegram API и intake/dispatch мокаются — юниты не бьют по сети."""
import json

import httpx
import pytest

from app.db import get_conn, schema
from app.doctor import dispatch, intake, poller


@pytest.fixture(autouse=True)
def reset_offset():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {schema()}.telegram_poll_state SET last_update_id = 0 WHERE id = 'singleton'")
        conn.commit()
    yield


def test_offset_roundtrip():
    with get_conn() as conn, conn.cursor() as cur:
        assert poller._get_last_offset(cur) == 0
        poller._save_offset(cur, 42)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        assert poller._get_last_offset(cur) == 42


def test_extract_file_id_photo_takes_largest():
    update = {"message": {"photo": [{"file_id": "small"}, {"file_id": "big"}]}}
    file_id, filename = poller._extract_file_id(update)
    assert file_id == "big"


def test_extract_file_id_document():
    update = {"message": {"document": {"file_id": "d1", "file_name": "analysis.pdf"}}}
    file_id, filename = poller._extract_file_id(update)
    assert file_id == "d1"
    assert filename == "analysis.pdf"


def test_extract_file_id_none_for_text():
    update = {"message": {"text": "привет"}}
    assert poller._extract_file_id(update) == (None, None)


def test_process_one_doctor_calls_handle_update_directly(monkeypatch):
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "doctor")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))
    monkeypatch.setattr(poller, "forward_to_capitan", lambda update: calls.setdefault("forwarded", update))

    update = {"update_id": 1, "message": {"text": "болит голова"}}
    poller.process_one(update)

    assert calls.get("update") == update
    assert "forwarded" not in calls


def test_process_one_other_forwards_not_handle_update(monkeypatch):
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "other")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))
    monkeypatch.setattr(poller, "forward_to_capitan", lambda update: calls.setdefault("forwarded", update))

    update = {"update_id": 2, "message": {"text": "запиши холестерин 5.5"}}
    poller.process_one(update)

    assert calls.get("forwarded") == update
    assert "update" not in calls


def test_process_one_dispatch_error_falls_back_to_forward(monkeypatch):
    calls = {}

    def boom(update):
        raise RuntimeError("classify failed")

    monkeypatch.setattr(dispatch, "route", boom)
    monkeypatch.setattr(poller, "forward_to_capitan", lambda update: calls.setdefault("forwarded", update))
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))

    update = {"update_id": 3, "message": {"text": "x"}}
    poller.process_one(update)

    assert calls.get("forwarded") == update
    assert "update" not in calls


def test_forward_to_capitan_text_only_no_multipart(monkeypatch):
    captured = {}

    def fake_post(url, data=None, files=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["files"] = files
        class R:
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(poller, "CAPITAN_RELAY_URL", "http://n8n:443/webhook/doctor-relay")
    monkeypatch.setattr(httpx, "post", fake_post)

    update = {"update_id": 5, "message": {"text": "запиши что-то"}}
    poller.forward_to_capitan(update)

    assert captured["files"] is None
    assert json.loads(captured["data"]["update"]) == update


def test_forward_to_capitan_with_photo_downloads_and_sends_multipart(monkeypatch):
    captured = {}

    def fake_post(url, data=None, files=None, timeout=None):
        captured["data"] = data
        captured["files"] = files
        class R:
            def raise_for_status(self): pass
        return R()

    def fake_download(file_id, timeout=20.0):
        captured["downloaded_file_id"] = file_id
        return b"fake-image-bytes"

    monkeypatch.setattr(poller, "CAPITAN_RELAY_URL", "http://n8n:443/webhook/doctor-relay")
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(poller.telegram, "download_file", fake_download)

    update = {"update_id": 6, "message": {"photo": [{"file_id": "ph1"}]}}
    poller.forward_to_capitan(update)

    assert captured["downloaded_file_id"] == "ph1"
    assert captured["files"]["data"][1] == b"fake-image-bytes"
    assert json.loads(captured["data"]["update"]) == update


def test_forward_to_capitan_no_relay_url_configured_logs_and_returns(monkeypatch):
    monkeypatch.setattr(poller, "CAPITAN_RELAY_URL", "")

    def boom(*a, **k):
        raise AssertionError("не должен пытаться слать без настроенного URL")

    monkeypatch.setattr(httpx, "post", boom)
    poller.forward_to_capitan({"update_id": 7, "message": {"text": "x"}})  # не бросает исключение
