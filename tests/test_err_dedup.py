"""app/err_dedup.py — порт n8n _Err Dedup (2026-09-21, последний шаг
«можно ли полностью убрать n8n»): три ночных cron-скрипта звали этот webhook
напрямую для алертов, единственная оставшаяся живая причина не выключать
n8n. app/err_dedup.py хардкодит card.err_dedup_state буквально (не через
schema()) — писал в БОЕВОЙ card даже под CARD_PG_SCHEMA=card_test.
Изолировано через _isolate_real_schema_writes (ROADMAP 0.7, 2026-09-24)."""
import pytest

from app import err_dedup as ed
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")

TEST_WF = "test_wf_err_dedup"
TEST_NODE = "test_node"


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
    from app import hermes_telegram  # 2026-09-21: алерты -> Hermes, не бот доктора
    sent = []
    monkeypatch.setattr(hermes_telegram, "send_message", lambda chat_id, text, parse_mode=None: sent.append((chat_id, text)))

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


# --- run_notify -> card.issue_log (2026-09-23, Шаг 1 «петли самоулучшения») --
# Единая точка входа и для alert_on_failure (фоновые циклы), и для трёх
# ночных cron-скриптов (/err-dedup) — см. докстринг run_notify().

def _read_issue(key):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT summary, occurrences FROM {schema()}.issue_log WHERE natural_key = %s", (key,))
        return cur.fetchone()


def test_run_notify_records_issue_on_valid_token():
    key = f"errdedup:{TEST_WF}:{TEST_NODE}"
    with get_conn() as conn, conn.cursor() as cur:
        ed.run_notify(cur, TEST_WF, TEST_NODE, "боевой сбой", False, ed.EXPECTED_TOKEN)
        conn.commit()
    assert _read_issue(key) == ("боевой сбой", 1)


def test_run_notify_records_even_when_telegram_suppressed_by_dedup():
    """Дедуп подавляет ТОЛЬКО Telegram — находка в бэклоге должна расти на
    каждое реальное срабатывание, иначе occurrences врёт про частоту."""
    key = f"errdedup:{TEST_WF}:{TEST_NODE}"
    with get_conn() as conn, conn.cursor() as cur:
        ed.run_notify(cur, TEST_WF, TEST_NODE, "первый", False, ed.EXPECTED_TOKEN)
        ed.run_notify(cur, TEST_WF, TEST_NODE, "второй, подавлен дедупом", False, ed.EXPECTED_TOKEN)
        conn.commit()
    row = _read_issue(key)
    assert row[1] == 2
    assert row[0] == "второй, подавлен дедупом"  # summary — самый свежий, не первый


def test_run_notify_wrong_token_does_not_record_issue():
    key = f"errdedup:{TEST_WF}:{TEST_NODE}"
    with get_conn() as conn, conn.cursor() as cur:
        ed.run_notify(cur, TEST_WF, TEST_NODE, "чужой токен", False, "неверный")
        conn.commit()
    assert _read_issue(key) is None
