"""app/small_webhooks.py — три мелких n8n-вебхука без расписания/LLM
(2026-09-20). check_breakfast — FakeCursor (health.meals не имеет тестовой
схемы-копии, тот же принцип, что context.py); action_ack пишет в реальный
health.action_log (прод-таблица) — изолировано через
_isolate_real_schema_writes (ROADMAP 0.7, 2026-09-24)."""
import pytest

from app import small_webhooks as sw
from app.db import get_conn

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


class FakeCursor:
    def __init__(self, queue):
        self.queue = list(queue)
        self._current = []

    def execute(self, query, params=None):
        self._current = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


# --- check_breakfast ----------------------------------------------------------

def test_check_breakfast_true_when_today_present(monkeypatch):
    from datetime import datetime, timedelta, timezone
    today_vl = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    cur = FakeCursor([[(today_vl,), ("2026-01-01",)]])
    assert sw.check_breakfast(cur) == {"breakfast_ready": True}


def test_check_breakfast_false_when_no_rows():
    cur = FakeCursor([[]])
    assert sw.check_breakfast(cur) == {"breakfast_ready": False}


def test_check_breakfast_false_when_only_older_dates():
    cur = FakeCursor([[("2020-01-01",)]])
    assert sw.check_breakfast(cur) == {"breakfast_ready": False}


# --- action_ack (пишет в реальный health.action_log) -------------------------

TEST_ACTION_ID = "2026-01-01|тестовое действие для test_small_webhooks"


def test_action_ack_wrong_token_forbidden():
    r = sw.action_ack("wrong", TEST_ACTION_ID, True)
    assert r == {"ok": False, "error": "forbidden"}


def test_action_ack_no_id():
    r = sw.action_ack(sw._ACTION_ACK_TOKEN, "", True)
    assert r == {"ok": False, "error": "no_id"}


def test_action_ack_writes_to_postgres_directly():
    """2026-09-20: раньше писало только в Sheets, PG отставал до суток —
    теперь прямой источник, дашборд видит отметку сразу."""
    r = sw.action_ack(sw._ACTION_ACK_TOKEN, TEST_ACTION_ID, True)
    assert r == {"ok": True, "error": None}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Date_Issued", "Title", "Done" FROM health.action_log WHERE "Action_ID" = %s', (TEST_ACTION_ID,))
        row = cur.fetchone()
    assert row == ("2026-01-01", "тестовое действие для test_small_webhooks", "да")


def test_action_ack_upserts_on_repeat_call():
    sw.action_ack(sw._ACTION_ACK_TOKEN, TEST_ACTION_ID, True)
    sw.action_ack(sw._ACTION_ACK_TOKEN, TEST_ACTION_ID, False)  # отметил и передумал
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Done" FROM health.action_log WHERE "Action_ID" = %s', (TEST_ACTION_ID,))
        assert cur.fetchone() == ("нет",)
        cur.execute('SELECT count(*) FROM health.action_log WHERE "Action_ID" = %s', (TEST_ACTION_ID,))
        assert cur.fetchone() == (1,)  # не задвоилось


def test_action_ack_title_with_pipe_preserved():
    aid = "2026-01-01|шаг 1 | шаг 2"
    sw.action_ack(sw._ACTION_ACK_TOKEN, aid, True)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Title" FROM health.action_log WHERE "Action_ID" = %s', (aid,))
        assert cur.fetchone() == ("шаг 1 | шаг 2",)


# --- виджет -------------------------------------------------------------------

def test_get_nutrition_widget_html_contains_new_endpoint():
    html = sw.get_nutrition_widget_html()
    assert "<!DOCTYPE html>" in html
    assert "/card/dashboard/today-nutrition" in html
    assert "webhook/today-nutrition" not in html  # старый n8n-путь не должен остаться
