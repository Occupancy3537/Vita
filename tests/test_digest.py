"""app/digest.py — вечерний дайджест (ROADMAP 5.5; сужен 2026-09-24 тикетом
«раскладка ботов по тематическим чатам» — anamnesis/nutrition_reports больше
не заходят сюда, см. докстринг модуля). Юниты на "всё накопленное — в порядке
постановки в очередь" и на "пустой дайджест не шлём вовсе"."""
import pytest

from app import digest, notify, timeutil
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def test_empty_digest_not_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    res = digest.build_and_send()
    assert res == {"sections": 0, "sent": False}
    assert sent == []


def test_accumulated_items_sent_in_queue_order(monkeypatch):
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)

    notify.notify("anomaly_detector", "normal", "БЛОК-ПЕРВЫЙ")
    notify.notify("monthly_trend", "normal", "БЛОК-ВТОРОЙ")

    res = digest.build_and_send()
    assert res["sections"] == 2
    assert sent[0].split("\n\n———\n\n") == ["БЛОК-ПЕРВЫЙ", "БЛОК-ВТОРОЙ"]


def test_delivered_items_not_included_twice(monkeypatch):
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)

    notify.notify("health_watchdog", "normal", "разовый нудж")
    digest.build_and_send()
    sent.clear()
    res = digest.build_and_send()  # тот же день, второй прогон — копить уже нечего
    assert res == {"sections": 0, "sent": False}
    assert sent == []


def test_critical_over_budget_lands_in_digest(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    for i in range(notify.CRITICAL_DAILY_BUDGET):
        notify.notify("gate_watch", "critical", f"в бюджете {i}")  # уходит немедленно, sent[i]
    notify.notify("gate_watch", "critical", "СВЕРХ БЮДЖЕТА")  # бюджет исчерпан — в notify_log, не в sent

    sent.clear()
    res = digest.build_and_send()
    assert res["sections"] == 1
    assert "СВЕРХ БЮДЖЕТА" in sent[0]
    for i in range(notify.CRITICAL_DAILY_BUDGET):
        assert f"в бюджете {i}" not in sent[0]  # немедленные не дублируются в дайджест


# ─────── «Стоп-кровь каналов» (2026-09-26, часть 1.1) — retry, не тихая потеря ───────

def test_failed_send_leaves_items_pending_for_next_run(monkeypatch):
    """Раньше _rest_blocks() помечал delivered_in_digest=true ДО отправки —
    неудачный send терял пункты навсегда. Теперь помечаем ТОЛЬКО при успехе."""
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: False)
    notify.notify("gate_watch", "normal", "пункт, который не должен потеряться")

    res = digest.build_and_send()
    assert res == {"sections": 1, "sent": False}

    # тот же день, второй прогон — теперь отправка удаётся, пункт доезжает
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: True)
    res2 = digest.build_and_send()
    assert res2 == {"sections": 1, "sent": True}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {schema()}.notify_log WHERE source = 'gate_watch' AND delivered_in_digest = true"
        )
        assert cur.fetchone()[0] == 1


def test_failed_send_does_not_write_digest_sent_log_row(monkeypatch):
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: False)
    notify.notify("gate_watch", "normal", "пункт")
    digest.build_and_send()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.notify_log WHERE source = 'digest_sent'")
        assert cur.fetchone()[0] == 0


def test_repeated_failures_escalate_via_alert_on_failure(monkeypatch):
    digest._consecutive_failures = 0
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: False)
    escalations = []
    monkeypatch.setattr(digest, "alert_on_failure", lambda source, exc: escalations.append((source, str(exc))))

    for i in range(digest.FAILURE_ALERT_THRESHOLD):
        notify.notify("gate_watch", "normal", f"пункт {i}")
        digest.build_and_send()

    assert escalations and escalations[-1][0] == "digest_delivery"
    digest._consecutive_failures = 0  # не протекает в другие тесты этого файла


def test_success_resets_failure_streak(monkeypatch):
    digest._consecutive_failures = digest.FAILURE_ALERT_THRESHOLD - 1
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: True)
    notify.notify("gate_watch", "normal", "пункт")
    digest.build_and_send()
    assert digest._consecutive_failures == 0


def test_anamnesis_and_nutrition_reports_no_longer_flow_through_digest(monkeypatch):
    """2026-09-24 (тикет «раскладка ботов по тематическим чатам»): оба источника
    доставляют себя сами (log_external_send), не через notify() — значит и не
    через _rest_blocks(). Явная регрессия на случай, если кто-то однажды снова
    случайно позовёт notify.notify("anamnesis", ...) / ("nutrition_reports", ...)."""
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    notify.log_external_send("anamnesis", "normal")
    notify.log_external_send("nutrition_reports", "normal")
    res = digest.build_and_send()
    assert res == {"sections": 0, "sent": False}  # log_external_send не пишет text -> нечего собирать
