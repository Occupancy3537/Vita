"""Step 2 плана нового доктора (§3.2) — poller.py: смещение оффсета,
маршрутизация "доктор -> напрямую / anamnesis/registrar -> напрямую / иначе
(TEST) -> ingest_test_message() -> /ingest в процессе (2026-09-21, #38/#43 —
раньше пересылалось в мёртвый n8n-Capitan и терялось, см. докстринг poller.py).
Telegram API и intake/dispatch мокаются — юниты не бьют по сети; ingest() сам
пишет в реальную card.source_message (та же схема, что test_main.py уже
использует для /ingest) — cleanup тестовых hash обязателен."""
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


def test_process_one_doctor_calls_handle_update_directly(monkeypatch):
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "doctor")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))
    monkeypatch.setattr(poller, "ingest_test_message", lambda update: calls.setdefault("ingested", update))

    update = {"update_id": 1, "message": {"text": "болит голова"}}
    poller.process_one(update)

    assert calls.get("update") == update
    assert "ingested" not in calls


def test_process_one_other_ingests_not_handle_update(monkeypatch):
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "other")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))
    monkeypatch.setattr(poller, "ingest_test_message", lambda update: calls.setdefault("ingested", update))

    update = {"update_id": 2, "message": {"text": "запиши холестерин 5.5"}}
    poller.process_one(update)

    assert calls.get("ingested") == update
    assert "update" not in calls


def test_process_one_registrar_calls_handle_update_directly(monkeypatch):
    # Волна 3 (B2): фото/документ -> registrar.handle_update в этом же процессе.
    from app import registrar

    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "registrar")
    monkeypatch.setattr(registrar, "handle_update", lambda update: calls.setdefault("registrar", update))
    monkeypatch.setattr(poller, "ingest_test_message", lambda update: calls.setdefault("ingested", update))

    update = {"update_id": 4, "message": {"photo": [{"file_id": "p"}]}}
    poller.process_one(update)

    assert calls.get("registrar") == update
    assert "ingested" not in calls


def test_process_one_dispatch_error_falls_back_to_ingest(monkeypatch):
    calls = {}

    def boom(update):
        raise RuntimeError("classify failed")

    monkeypatch.setattr(dispatch, "route", boom)
    monkeypatch.setattr(poller, "ingest_test_message", lambda update: calls.setdefault("ingested", update))
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))

    update = {"update_id": 3, "message": {"text": "x"}}
    poller.process_one(update)

    assert calls.get("ingested") == update
    assert "update" not in calls


def test_ingest_test_message_writes_source_message():
    """Живой путь (2026-09-21, #38/#43): TEST-текст реально долетает до
    card.source_message через тот же /ingest, что и всё остальное — не
    теряется на мёртвом Capitan-релее."""
    update = {"update_id": 100, "message": {"text": "запиши тест_poller_ingest холестерин 5.5 ммоль/л"}}
    poller.ingest_test_message(update)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT channel, status FROM {schema()}.source_message WHERE raw_text = %s",
            ("запиши тест_poller_ingest холестерин 5.5 ммоль/л",),
        )
        row = cur.fetchone()
    assert row == ("telegram", "received")


def test_ingest_test_message_uses_caption_when_no_text():
    update = {"update_id": 101, "message": {"caption": "тест_poller_ingest_caption фото анализа"}}
    poller.ingest_test_message(update)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT channel FROM {schema()}.source_message WHERE raw_text = %s",
            ("тест_poller_ingest_caption фото анализа",),
        )
        assert cur.fetchone() == ("telegram",)


def test_ingest_test_message_no_text_notifies_owner(monkeypatch, notify_capture):
    poller.ingest_test_message({"update_id": 102, "message": {}})
    assert len(notify_capture) == 1
    assert "НЕ сохранено" in notify_capture[0][1]


def test_ingest_test_message_ingest_failure_notifies_owner(monkeypatch, notify_capture):
    def boom(req):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.main.ingest", boom)
    poller.ingest_test_message({"update_id": 103, "message": {"text": "тест_poller_ingest_fail"}})
    assert len(notify_capture) == 1
    assert "тест_poller_ingest_fail" in notify_capture[0][1]


# ─────────────────────────── Волна 1 (A3, 2026-09-17) ───────────────────────────

@pytest.fixture()
def notify_capture(monkeypatch):
    """Мок telegram.send_message: пишем (chat_id, text) в список; сбрасываем анти-спам."""
    sent = []

    def fake_send(chat_id, text, *a, **k):
        sent.append((chat_id, text))

    monkeypatch.setattr(poller.telegram, "send_message", fake_send)
    poller._last_loss_notify_ts = 0.0
    yield sent
    poller._last_loss_notify_ts = 0.0


def test_safe_process_swallows_exception_and_continues(monkeypatch):
    """Guard: исключение в process_one глотается с logger.exception, не бросается наружу."""
    def boom(update):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(poller, "process_one", boom)
    poller._safe_process({"update_id": 10, "message": {"text": "x"}})  # не бросает


def test_safe_process_passes_update_through(monkeypatch):
    seen = []
    monkeypatch.setattr(poller, "process_one", lambda u: seen.append(u))
    poller._safe_process({"update_id": 11})
    assert seen == [{"update_id": 11}]


def test_notify_owner_lost_includes_summary(notify_capture):
    poller._notify_owner_lost({"update_id": 20, "message": {"text": "завтрак: овсянка с ягодами и семенами льна, чай"}})
    assert len(notify_capture) == 1
    chat_id, text = notify_capture[0]
    assert chat_id == "8956401"
    assert "НЕ сохранено" in text
    assert "овсянка" in text


def test_notify_owner_lost_photo_summary(notify_capture):
    poller._notify_owner_lost({"update_id": 21, "message": {"photo": [{"file_id": "p"}]}})
    chat_id, text = notify_capture[0]
    assert "фото" in text


def test_notify_owner_lost_cooldown_6h(notify_capture):
    """Анти-спам: вторая потеря в пределах 6 ч НЕ шлёт новое сообщение;
    после «прошедших» 6 ч — шлёт."""
    poller._notify_owner_lost({"update_id": 30, "message": {"text": "первое потерянное"}})
    poller._notify_owner_lost({"update_id": 31, "message": {"text": "второе потерянное"}})
    assert len(notify_capture) == 1  # второе подавлено кулдауном

    poller._last_loss_notify_ts -= poller.LOSS_NOTIFY_COOLDOWN + 1  # «прошло больше 6 часов»
    poller._notify_owner_lost({"update_id": 32, "message": {"text": "третье потерянное"}})
    assert len(notify_capture) == 2


def test_notify_owner_send_failure_never_raises(monkeypatch, caplog):
    """Падение самой отправки нотификации не должно ронять цикл."""
    poller._last_loss_notify_ts = 0.0

    def boom(chat_id, text, *a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(poller.telegram, "send_message", boom)
    poller._notify_owner_lost({"update_id": 50, "message": {"text": "x"}})  # не бросает
    poller._last_loss_notify_ts = 0.0
