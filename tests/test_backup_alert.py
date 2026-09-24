"""app/backup_alert.py — порт n8n `_Backup Alert` (2026-09-20). health.
backup_alert_state — реальная прод-таблица (1 строка-синглтон).

2026-09-22 (найдено при наполнении страницы настроек): фикстура раньше
УДАЛЯЛА боевую строку до/после каждого теста — каждый прогон pytest стирал
реальное состояние последнего пинга бэкапа (тот же класс, что инцидент с
health.anomaly_log, #54), из-за чего check_stale() после каждого прогона
тестов честно решал, что «пинга не было никогда», и слал бы ложный алерт.
Тогда починили save→delete→restore.

2026-09-24 (ROADMAP 0.7): save/restore заменён на `_isolate_real_schema_writes`
(tests/conftest.py) — соединение с боевой базой физически не может
закоммитить ничего, поэтому восстанавливать больше нечего: DELETE ниже —
не риск, а просто способ дать тестам предсказуемую стартовую точку («пинга
ещё не было») внутри их же незакоммиченной транзакции."""
import pytest

from app import backup_alert as ba
from app.db import get_conn

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


@pytest.fixture(autouse=True)
def start_empty(_isolate_real_schema_writes):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM health.backup_alert_state WHERE id = 1")
        conn.commit()
    yield


def test_handle_ping_wrong_token_is_silent_and_does_not_store():
    assert ba.handle_ping("wrong", "ok", "", "2026-09-20 04:00") == ""
    with get_conn() as conn, conn.cursor() as cur:
        assert ba._load_state(cur) is None


def test_handle_ping_ok_result_no_alert_but_stores():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "ok", "", "2026-09-20 04:00")
    assert text == ""
    with get_conn() as conn, conn.cursor() as cur:
        state = ba._load_state(cur)
    assert state["result"] == "ok"


def test_handle_ping_failed_result_alerts():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "failed", "rclone timeout", "2026-09-20 04:00")
    assert "СБОЙ" in text
    assert "rclone timeout" in text


def test_handle_ping_partial_result_alerts_softly():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "partial", "gdrive auth expired", "2026-09-20 04:00")
    assert "один провайдер не сработал" in text
    assert "gdrive auth expired" in text


def test_handle_ping_unknown_result_alerts():
    text = ba.handle_ping(ba.WEBHOOK_TOKEN, "weird", "?", "2026-09-20 04:00")
    assert "weird" in text


def test_check_stale_never_pinged():
    assert "никогда" in ba.check_stale()


def test_check_stale_recent_ok_ping_is_silent():
    ba.handle_ping(ba.WEBHOOK_TOKEN, "ok", "", "now")
    assert ba.check_stale() == ""


def test_check_stale_old_ping_alerts(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO health.backup_alert_state (id, ts, result, detail, stamp) "
            "VALUES (1, now() - interval '30 hours', 'ok', '', 'вчера')"
        )
        conn.commit()
    assert "не отработал" in ba.check_stale()


def test_check_stale_recent_but_failed_result_alerts():
    ba.handle_ping(ba.WEBHOOK_TOKEN, "failed", "diskfull", "2026-09-20 04:00")
    assert "последний прогон со сбоем" in ba.check_stale()


def test_run_once_sends_only_on_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(ba, "check_stale", lambda: "⚠️ тест")
    monkeypatch.setattr(ba.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    ba.run_once()
    assert calls == [(("backup_alert", "critical", "⚠️ тест"), {"parse_mode": "HTML"})]


def test_run_once_no_send_when_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(ba, "check_stale", lambda: "")
    monkeypatch.setattr(ba.notify, "notify", lambda *a, **kw: calls.append((a, kw)))
    ba.run_once()
    assert calls == []
