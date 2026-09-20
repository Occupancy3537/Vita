"""app/memory_archive_check.py — порт n8n `_Memory Pre-Archive Check` (2026-09-20)."""
from app import memory_archive_check as mac


def test_plural_ru():
    assert mac._plural_ru(1, "эпизод", "эпизода", "эпизодов") == "эпизод"
    assert mac._plural_ru(2, "эпизод", "эпизода", "эпизодов") == "эпизода"
    assert mac._plural_ru(5, "эпизод", "эпизода", "эпизодов") == "эпизодов"
    assert mac._plural_ru(11, "эпизод", "эпизода", "эпизодов") == "эпизодов"
    assert mac._plural_ru(21, "эпизод", "эпизода", "эпизодов") == "эпизод"


def test_build_alert_empty_when_no_w3_questions(monkeypatch):
    monkeypatch.setattr(mac, "run_pre_archive_check", lambda cur: [
        {"episode_id": 1, "symptom_key": "x", "action": "archived", "reason": "тишина"},
    ])
    assert mac.build_alert(None) == ""


def test_build_alert_lists_w3_questions(monkeypatch):
    monkeypatch.setattr(mac, "run_pre_archive_check", lambda cur: [
        {"episode_id": 1, "symptom_key": "x", "action": "archived", "reason": "тишина"},
        {"episode_id": 2, "symptom_key": "онемение", "action": "w3_question", "reason": "критический паттерн"},
    ])
    text = mac.build_alert(None)
    assert "1 эпизод" in text
    assert "онемение (id 2) — критический паттерн" in text
    assert "x (id 1)" not in text  # archived-эпизоды не попадают в текст


def test_run_once_sends_telegram_only_when_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "_build_alert", lambda: "🗂 тест")
    monkeypatch.setattr(mac.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    mac.run_once()
    assert len(calls) == 1
    assert calls[0][0] == (mac.CHAT_ID, "🗂 тест")
    assert calls[0][1] == {"parse_mode": "HTML"}


def test_run_once_no_send_when_no_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "_build_alert", lambda: "")
    monkeypatch.setattr(mac.telegram, "send_message", lambda *a, **kw: calls.append((a, kw)))
    mac.run_once()
    assert calls == []
