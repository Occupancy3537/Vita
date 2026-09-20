"""app/weekly_advisor.py — порт n8n Weekly AI Advisor (2026-09-20, группа 2,
последний и самый крупный порт волны). Юниты на чистые хелперы + parse/sync
сценарии через monkeypatch (тот же принцип, что test_nutrition_reports.py);
write_recommendations_log — интеграционный тест на реальную таблицу с явным
cleanup тестового Date, не пересекающегося с реальными данными."""
import pytest

from app import weekly_advisor as wa
from app.db import get_conn


# --- чистые хелперы -----------------------------------------------------------

def test_r1_rounds_to_one_decimal():
    assert wa._r1(24.349) == 24.3
    assert wa._r1(None) is None


def test_avg_ignores_none_and_empty():
    assert wa._avg([1, 2, None, 3]) == 2
    assert wa._avg([]) is None
    assert wa._avg([None, None]) is None


def test_prev_weekly_skips_today_and_non_weekly():
    recs = [
        {"Date": "2026-09-20", "Period_Type": "weekly", "Status": "отправлено"},
        {"Date": "2026-09-13", "Period_Type": "weekly", "Status": "без_действий"},
        {"Date": "2026-09-19", "Period_Type": "daily", "Status": "отправлено"},
    ]
    prev = wa._prev_weekly(recs, "2026-09-20")
    assert prev["Date"] == "2026-09-13"


def test_prev_weekly_none_when_no_history():
    assert wa._prev_weekly([], "2026-09-20") is None
    assert wa._prev_weekly([{"Date": "2026-09-20", "Period_Type": "weekly"}], "2026-09-20") is None


def test_prev_weekly_picks_most_recent_of_several():
    recs = [
        {"Date": "2026-09-06", "Period_Type": "weekly", "Status": "отправлено"},
        {"Date": "2026-09-13", "Period_Type": "weekly", "Status": "отправлено"},
        {"Date": "2026-08-30", "Period_Type": "weekly", "Status": "отправлено"},
    ]
    prev = wa._prev_weekly(recs, "2026-09-20")
    assert prev["Date"] == "2026-09-13"


# --- call_model ---------------------------------------------------------------

def test_call_model_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert wa.call_model("тест") == ""


# --- parse_advisor_response -----------------------------------------------------

BASE_CTX = {"window": {"to": "2026-09-20"}, "restrictions_unknown": False, "active_restrictions": []}


def _raw(actions):
    import json as _json
    return "Разбор недели.\n\n<<<ACTIONS\n" + _json.dumps({"actions": actions}) + "\nACTIONS>>>"


def test_parse_valid_action_kept():
    raw = _raw([{"title": "Больше сна", "why": "низкий HRV", "type": "sleep"}])
    row = wa.parse_advisor_response(raw, BASE_CTX, [], None)
    assert row["Status"] == "отправлено"
    assert len(row["actions"]) == 1
    assert row["actions"][0]["title"] == "Больше сна"
    assert row["Has_Alert"] is False


def test_parse_missing_block_flags_alert_and_no_actions():
    row = wa.parse_advisor_response("Просто текст без машинного блока.", BASE_CTX, [], None)
    assert row["Status"] == "без_действий"
    assert row["actions"] == []
    assert row["Has_Alert"] is True


def test_parse_empty_text_raises():
    with pytest.raises(ValueError):
        wa.parse_advisor_response("   ", BASE_CTX, [], None)


def test_parse_filters_load_action_when_restriction_active():
    ctx = {
        "window": {"to": "2026-09-20"}, "restrictions_unknown": False,
        "active_restrictions": [{"contra_load": "бег, прыжки", "status": "active", "allowed": "ходьба"}],
    }
    raw = _raw([
        {"title": "Силовая на ноги", "type": "load_high", "why": "прогресс"},
        {"title": "Прогулка 30 мин", "type": "walk", "why": "низкая нагрузка"},
    ])
    row = wa.parse_advisor_response(raw, ctx, [], None)
    titles = [a["title"] for a in row["actions"]]
    assert "Силовая на ноги" not in titles
    assert "Прогулка 30 мин" in titles
    assert "гейт" in row["Based_On"]


def test_parse_restrictions_unknown_drops_unknown_type_actions():
    ctx = {"window": {"to": "2026-09-20"}, "restrictions_unknown": True, "active_restrictions": []}
    raw = _raw([{"title": "Что-то неясное", "type": "totally_bogus", "why": "..."}])
    row = wa.parse_advisor_response(raw, ctx, [], None)
    assert row["actions"] == []
    assert any("Patient_State" in a or "профиль" in a for a in [row["Alert_Text"]])


def test_parse_truncates_to_three_actions():
    raw = _raw([{"title": f"Действие {i}", "type": "other", "why": "тест"} for i in range(5)])
    row = wa.parse_advisor_response(raw, BASE_CTX, [], None)
    assert len(row["actions"]) == 3


def test_parse_escalates_on_second_no_actions_week():
    prev = {"Date": "2026-09-13", "Period_Type": "weekly", "Status": "без_действий"}
    row = wa.parse_advisor_response("Текст без блока.", BASE_CTX, [], prev)
    assert row["Status"] == "без_действий_эскалация"
    assert "ЭСКАЛАЦИЯ" in row["Telegram_Text"]


def test_parse_no_escalation_when_prev_had_actions():
    prev = {"Date": "2026-09-13", "Period_Type": "weekly", "Status": "отправлено"}
    row = wa.parse_advisor_response("Текст без блока.", BASE_CTX, [], prev)
    assert row["Status"] == "без_действий"
    assert "ЭСКАЛАЦИЯ" not in row["Telegram_Text"]


def test_parse_priority_high_on_strong_anomalies():
    ctx = dict(BASE_CTX, anomalies_last_7d=[{"label": "RHR вверх", "strong": 1} for _ in range(3)])
    row = wa.parse_advisor_response(_raw([{"title": "Отдых", "type": "sleep", "why": "тест"}]), ctx, [], None)
    assert row["Priority"] == "высокий"


# --- sync_actions_to_card --------------------------------------------------

def test_sync_actions_accepted_normal_priority(monkeypatch):
    from app import recommendations as rc

    class FakeResp:
        accepted = True
        id = "rc_test123"
        priority = "normal"
        rejected_gate = None
        rejected_reason = None

    monkeypatch.setattr(rc, "propose_recommendation", lambda req: FakeResp())
    summary = wa.sync_actions_to_card([{"title": "Больше воды", "type": "other", "why": "тест"}], "2026-09-20")
    assert summary == "✅ Больше воды (rc_test123)"


def test_sync_actions_rejected_by_gate(monkeypatch):
    from app import recommendations as rc

    class FakeResp:
        accepted = False
        id = None
        priority = None
        rejected_gate = "G4"
        rejected_reason = "противоречит гейту"

    monkeypatch.setattr(rc, "propose_recommendation", lambda req: FakeResp())
    summary = wa.sync_actions_to_card([{"title": "Бег", "type": "load_high", "why": "тест"}], "2026-09-20")
    assert summary == "⛔ Бег — заблокировано (G4: противоречит гейту)"


def test_sync_actions_card_unavailable_reports_warning(monkeypatch):
    from app import recommendations as rc

    def boom(req):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(rc, "propose_recommendation", boom)
    summary = wa.sync_actions_to_card([{"title": "Что-то", "type": "other", "why": "тест"}], "2026-09-20")
    assert "⚠️ Что-то — card-service недоступен" in summary


def test_sync_actions_empty_list_returns_empty_string():
    assert wa.sync_actions_to_card([], "2026-09-20") == ""


# --- write_recommendations_log (интеграционный, реальная таблица) --------------

TEST_DATE = "1999-12-31"


@pytest.fixture(autouse=True)
def _cleanup_test_row():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.recommendations_log WHERE "Date" = %s', (TEST_DATE,))
        conn.commit()


def test_write_recommendations_log_inserts_then_overwrites_same_date():
    row1 = {
        "Date": TEST_DATE, "Period_Type": "weekly", "Recommendation_Text": "первый текст",
        "Based_On": "тест", "Status": "отправлено", "Priority": "низкий",
        "Telegram_Text": "первый", "Alert_Text": "", "Has_Alert": False,
    }
    with get_conn() as conn, conn.cursor() as cur:
        wa.write_recommendations_log(cur, row1)
        conn.commit()

    row2 = dict(row1, Recommendation_Text="второй текст (перезапись)", Telegram_Text="второй", Has_Alert=True)
    with get_conn() as conn, conn.cursor() as cur:
        wa.write_recommendations_log(cur, row2)
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Recommendation_Text", "Has_Alert" FROM health.recommendations_log WHERE "Date" = %s', (TEST_DATE,))
        rows = cur.fetchall()
    assert len(rows) == 1, "DELETE+INSERT по Date+Period_Type не должен плодить дубли при повторном прогоне"
    assert rows[0][0] == "второй текст (перезапись)"
    assert rows[0][1] == "TRUE"


# --- run_once (полностью замоканная оркестрация) -------------------------------

def test_run_once_sends_telegram_and_writes_log(monkeypatch):
    src = {"recs": [], "targets": []}
    monkeypatch.setattr(wa, "_fetch_all", lambda cur: src)
    monkeypatch.setattr(wa, "build_context", lambda s: {"window": {"to": "2026-09-20"}})
    monkeypatch.setattr(wa, "build_prompt", lambda ctx: "промпт")
    monkeypatch.setattr(wa, "call_model", lambda prompt: "текст\n\n<<<ACTIONS\n{\"actions\": []}\nACTIONS>>>")
    monkeypatch.setattr(wa, "sync_actions_to_card", lambda actions, date: "")

    written = {}
    monkeypatch.setattr(wa, "write_recommendations_log", lambda cur, row: written.update(row))

    sent = []
    monkeypatch.setattr(wa.telegram, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    wa.run_once()

    assert written["Date"] == "2026-09-20"
    assert sent and sent[0][0] == wa.CHAT_ID
    assert "2026-09-20" in sent[0][1]


def test_run_once_model_silent_skips_write(monkeypatch):
    monkeypatch.setattr(wa, "_fetch_all", lambda cur: {"recs": [], "targets": []})
    monkeypatch.setattr(wa, "build_context", lambda s: {"window": {"to": "2026-09-20"}})
    monkeypatch.setattr(wa, "build_prompt", lambda ctx: "промпт")
    monkeypatch.setattr(wa, "call_model", lambda prompt: "")

    called = []
    monkeypatch.setattr(wa, "write_recommendations_log", lambda cur, row: called.append(row))
    sent = []
    monkeypatch.setattr(wa.telegram, "send_message", lambda chat_id, text: sent.append(text))

    wa.run_once()

    assert called == []
    assert sent  # предупредили в Telegram, что модель не ответила
