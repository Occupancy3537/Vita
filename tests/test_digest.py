"""app/digest.py — вечерний дайджест (ROADMAP 5.5). Юниты на порядок сборки
(анамнез первым, питание вторым, остальное — в порядке накопления) и на
"пустой дайджест не шлём вовсе"."""
import pytest

from app import digest, notify, timeutil
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def test_empty_digest_not_sent(monkeypatch):
    monkeypatch.setattr(digest.anamnesis, "ask_daily", lambda: {"action": "done"})
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)
    res = digest.build_and_send()
    assert res == {"sections": 0, "sent": False}
    assert sent == []


def test_anamnesis_first_nutrition_second_rest_after(monkeypatch):
    monkeypatch.setattr(digest.anamnesis, "ask_daily", lambda: {"action": "ask", "text": "БЛОК-АНАМНЕЗ"})
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)

    # порядок постановки в очередь НАМЕРЕННО перепутан — "остальное" раньше
    # nutrition_reports, дайджест обязан всё равно поставить nutrition вторым.
    notify.notify("anomaly_detector", "normal", "БЛОК-ОСТАЛЬНОЕ")
    notify.notify("nutrition_reports", "normal", "БЛОК-ПИТАНИЕ")

    res = digest.build_and_send()
    assert res["sections"] == 3
    assert sent[0].split("\n\n———\n\n") == ["БЛОК-АНАМНЕЗ", "БЛОК-ПИТАНИЕ", "БЛОК-ОСТАЛЬНОЕ"]


def test_delivered_items_not_included_twice(monkeypatch):
    monkeypatch.setattr(digest.anamnesis, "ask_daily", lambda: {"action": "done"})
    sent = []
    monkeypatch.setattr(digest.notify, "_send", lambda text, parse_mode=None: sent.append(text) or True)

    notify.notify("health_watchdog", "normal", "разовый нудж")
    digest.build_and_send()
    sent.clear()
    res = digest.build_and_send()  # тот же день, второй прогон — копить уже нечего
    assert res == {"sections": 0, "sent": False}
    assert sent == []


def test_critical_over_budget_lands_in_digest(monkeypatch):
    monkeypatch.setattr(digest.anamnesis, "ask_daily", lambda: {"action": "done"})
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
