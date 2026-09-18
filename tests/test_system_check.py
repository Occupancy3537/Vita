"""app/system_check.py — порт n8n `_System Check` (2026-09-19, см. докстринг
модуля для причины переноса именно этого воркфлоу первым). httpx мокается
(тот же паттерн, что test_doctor_context.py) — тесты не бьют по n8n/сети;
Postgres-часть — реальная health.* схема (общая, без изоляции, только чтение,
тот же принцип, что test_dashboard.py)."""
from datetime import date, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from app import system_check


def _resp(url="http://test/", **kwargs):
    """httpx.Response без прикреплённого request не даёт вызвать raise_for_status
    (AttributeError, а не HTTPStatusError) — в проверяемом коде это тихо попадает
    в except и превращается в "не отвечает", маскируя настоящий сценарий теста."""
    return httpx.Response(request=httpx.Request("GET", url), **kwargs)


def test_check_load_gate_blocked_with_walking_verdict_is_clean():
    problems, notes = [], []
    cache = {"decision": {"gate": {"blocked": True, "source": "Patient_State"}, "verdict": "ходьба и плавание"}}
    system_check._check_load_gate(cache, problems, notes)
    assert problems == [] and notes == []


def test_check_load_gate_open_is_a_problem():
    problems, notes = [], []
    cache = {"decision": {"gate": {"blocked": False, "source": "Patient_State"}, "verdict": "бег"}}
    system_check._check_load_gate(cache, problems, notes)
    assert len(problems) == 1
    assert "ГЕЙТ НАГРУЗКИ ОТКРЫТ" in problems[0]


def test_check_load_gate_missing_is_a_note_not_a_problem():
    problems, notes = [], []
    system_check._check_load_gate({}, problems, notes)
    assert problems == []
    assert len(notes) == 1


def test_check_load_gate_blocked_but_wrong_verdict_is_a_problem():
    problems, notes = [], []
    cache = {"decision": {"gate": {"blocked": True}, "verdict": "силовая тренировка"}}
    system_check._check_load_gate(cache, problems, notes)
    assert len(problems) == 1
    assert "не про ходьбу/плавание" in problems[0]


def test_check_n8n_active_all_present_is_clean(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        names = system_check.EXPECTED_ACTIVE_N8N
        return _resp(url, status_code=200, json={"data": [{"name": n, "active": True} for n in names]})
    monkeypatch.setattr(httpx, "get", fake_get)
    problems, notes = [], []
    system_check._check_n8n_active(problems, notes)
    assert problems == []


def test_check_n8n_active_missing_one_is_a_problem(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        names = [n for n in system_check.EXPECTED_ACTIVE_N8N if n != "Health Watchdog"]
        return _resp(url, status_code=200, json={"data": [{"name": n, "active": True} for n in names]})
    monkeypatch.setattr(httpx, "get", fake_get)
    problems, notes = [], []
    system_check._check_n8n_active(problems, notes)
    assert len(problems) == 1
    assert "Health Watchdog" in problems[0]


def test_check_n8n_active_request_fails_is_a_note_not_a_problem(monkeypatch):
    def fake_get(*a, **k):
        raise httpx.ConnectError("сеть легла")
    monkeypatch.setattr(httpx, "get", fake_get)
    problems, notes = [], []
    system_check._check_n8n_active(problems, notes)
    assert problems == []
    assert len(notes) == 1


def test_missing_dates_reports_gap():
    cur = MagicMock()
    today = system_check._vl_now().date()
    # только позавчерашняя дата есть — остальные 3-6 дней назад отсутствуют
    cur.fetchall.return_value = [(today - timedelta(days=2),)]
    gap = system_check._missing_dates(cur, "daily_trends", "Дата", 6, [], "Daily_Trends")
    expected_missing = {(today - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(3, 7)}
    assert gap == expected_missing


def test_missing_dates_empty_result_is_a_note_not_a_gap():
    cur = MagicMock()
    cur.fetchall.return_value = []
    notes = []
    gap = system_check._missing_dates(cur, "day_sum", "Date", 6, notes, "day_sum")
    assert gap == set()
    assert len(notes) == 1


def test_missing_dates_no_gap_when_all_present():
    cur = MagicMock()
    today = system_check._vl_now().date()
    cur.fetchall.return_value = [(today - timedelta(days=k),) for k in range(2, 7)]
    gap = system_check._missing_dates(cur, "daily_trends", "Дата", 6, [], "Daily_Trends")
    assert gap == set()


def test_build_message_all_clean_no_problems(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "api/v1/workflows" in url:
            return _resp(url, status_code=200, json={"data": [{"name": n, "active": True} for n in system_check.EXPECTED_ACTIVE_N8N]})
        if "today-dashboard" in url:
            return _resp(url, status_code=200, json={
                "updated_at": system_check._vl_now().isoformat(),
                "decision": {"gate": {"blocked": True}, "verdict": "ходьба и плавание"},
            })
        # именно виджет-URL начинается с "dashboard" — "today-dashboard"/"bioage-dashboard"
        # проверены выше, иначе оба тоже совпали бы с "dashboard?token" как подстрокой.
        if url.startswith(system_check._N8N_BASE + "dashboard?token"):
            return _resp(url, status_code=200, text="<!doctype html><html></html>")
        return _resp(url, status_code=200, json={"updated_at": system_check._vl_now().isoformat()})
    monkeypatch.setattr(httpx, "get", fake_get)

    msg = system_check.build_message()
    assert msg["has_problems"] is False
    assert "проблемы" not in msg["message"]


def test_build_message_surfaces_a_problem(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "api/v1/workflows" in url:
            return _resp(url, status_code=200, json={"data": []})  # ничего не активно — верный сигнал проблемы
        if "today-dashboard" in url:
            return _resp(url, status_code=200, json={
                "updated_at": system_check._vl_now().isoformat(),
                "decision": {"gate": {"blocked": True}, "verdict": "ходьба и плавание"},
            })
        if url.startswith(system_check._N8N_BASE + "dashboard?token"):
            return _resp(url, status_code=200, text="<!doctype html><html></html>")
        return _resp(url, status_code=200, json={"updated_at": system_check._vl_now().isoformat()})
    monkeypatch.setattr(httpx, "get", fake_get)

    msg = system_check.build_message()
    assert msg["has_problems"] is True
    assert "НЕ АКТИВНЫ" in msg["message"]


def test_run_once_sends_telegram_message(monkeypatch):
    monkeypatch.setattr(system_check, "build_message", lambda: {"message": "тест", "has_problems": False, "problems": [], "notes": []})
    calls = []
    monkeypatch.setattr(system_check.telegram, "send_message", lambda chat_id, text, parse_mode=None: calls.append((chat_id, text, parse_mode)))
    system_check.run_once()
    assert calls == [(system_check.CHAT_ID, "тест", "HTML")]
