"""app/err_dedup.py — порт n8n _Err Dedup (2026-09-21, последний шаг
«можно ли полностью убрать n8n»): три ночных cron-скрипта звали этот webhook
напрямую для алертов, единственная оставшаяся живая причина не выключать
n8n. Реальная таблица card.err_dedup_state, тестовые ключи, cleanup."""
import pytest

from app import err_dedup as ed
from app.db import get_conn


TEST_WF = "test_wf_err_dedup"
TEST_NODE = "test_node"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM card.err_dedup_state WHERE key LIKE %s", (f"{TEST_WF}%",))
        conn.commit()


def test_wrong_token_returns_send_false_silently():
    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "ошибка", token="неверный")
    assert result == {"send": False, "text": "", "silent": True, "burst": 0}


def test_first_call_sends():
    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "🔴 сбой", token=ed.EXPECTED_TOKEN)
        conn.commit()
    assert result["send"] is True
    assert "сбой" in result["text"]


def test_second_call_within_window_is_suppressed():
    with get_conn() as conn, conn.cursor() as cur:
        ed.check_and_notify(cur, TEST_WF, TEST_NODE, "первая", token=ed.EXPECTED_TOKEN)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "вторая", token=ed.EXPECTED_TOKEN)
        conn.commit()
    assert result["send"] is False
    assert result["burst"] == 1


def test_escapes_html_special_characters():
    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "Ошибка: <script> & test", token=ed.EXPECTED_TOKEN)
    assert "&lt;script&gt;" in result["text"]
    assert "&amp;" in result["text"]


def test_default_text_when_telegram_field_empty():
    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "", token=ed.EXPECTED_TOKEN)
    assert TEST_WF in result["text"] and TEST_NODE in result["text"]


def test_burst_nudge_fires_after_55_minutes_of_suppression(monkeypatch):
    from datetime import datetime, timedelta, timezone

    with get_conn() as conn, conn.cursor() as cur:
        ed.check_and_notify(cur, TEST_WF, TEST_NODE, "первая", token=ed.EXPECTED_TOKEN)
        conn.commit()

    # искусственно откатываем last_notified_at на 56 минут назад и выставляем burst>0,
    # как будто было несколько подавленных вызовов подряд
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE card.err_dedup_state SET last_notified_at = %s, burst_count = 3 WHERE key = %s",
            (datetime.now(timezone.utc) - timedelta(minutes=56), f"{TEST_WF}|{TEST_NODE}"),
        )
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        result = ed.check_and_notify(cur, TEST_WF, TEST_NODE, "ещё сбой", token=ed.EXPECTED_TOKEN)
        conn.commit()

    assert result["send"] is True
    assert "серия ошибок продолжается" in result["text"]
    assert "3 за ~час" in result["text"]


def test_run_notify_sends_telegram_only_when_send_true(monkeypatch):
    from app.doctor import telegram
    sent = []
    monkeypatch.setattr(telegram, "send_message", lambda chat_id, text, parse_mode=None: sent.append((chat_id, text)))

    with get_conn() as conn, conn.cursor() as cur:
        ed.run_notify(cur, TEST_WF, TEST_NODE, "первый сбой", False, ed.EXPECTED_TOKEN)
        conn.commit()
    assert len(sent) == 1
    assert sent[0][0] == ed.CHAT_ID

    sent.clear()
    with get_conn() as conn, conn.cursor() as cur:
        ed.run_notify(cur, TEST_WF, TEST_NODE, "второй сбой", False, ed.EXPECTED_TOKEN)
        conn.commit()
    assert sent == []  # подавлено — Telegram не звался вообще
