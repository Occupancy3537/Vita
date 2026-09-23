"""Step 2 плана нового доктора (§3.2) — poller.py: смещение оффсета,
маршрутизация "доктор -> напрямую / anamnesis/registrar -> напрямую / иначе
(TEST) -> ingest_test_message() -> /ingest в процессе (2026-09-21, #38/#43 —
раньше пересылалось в мёртвый n8n-Capitan и терялось, см. докстринг poller.py).
Telegram API и intake/dispatch мокаются — юниты не бьют по сети; ingest() сам
пишет в реальную card.source_message (та же схема, что test_main.py уже
использует для /ingest) — cleanup тестовых hash обязателен."""
import pytest

from app.db import get_conn, schema
from app.doctor import dispatch, gate, intake, poller
from app.redflag_b import LayerBResult


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


def test_process_one_ignores_foreign_chat_id(monkeypatch):
    """2026-09-22 (внешний аудит, K5 — КРИТИЧНО): чужой chat_id не должен
    доходить ни до dispatch, ни до intake — иначе кто угодно, нашедший бота,
    обрабатывался бы как пациент с полным досье Влада в контексте."""
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: calls.setdefault("routed", True))
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))

    update = {"update_id": 1, "message": {"chat": {"id": 999999}, "text": "чужое сообщение"}}
    poller.process_one(update)

    assert calls == {}


def test_process_one_accepts_owner_chat_id(monkeypatch):
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "doctor")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))

    update = {"update_id": 2, "message": {"chat": {"id": 8956401}, "text": "болит голова"}}
    poller.process_one(update)

    assert calls.get("update") == update


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
    """Guard: исключение в process_one глотается с logger.exception, не бросается наружу.
    F1 (внешний аудит логики, 2026-09-22): потеря апдейта дополнительно уходит
    алертом владельцу через alert_on_failure — здесь мок, чтобы не слать в Telegram."""
    alerts = []

    def boom(update):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(poller, "process_one", boom)
    monkeypatch.setattr(poller, "alert_on_failure", lambda src, exc: alerts.append((src, exc)))
    poller._safe_process({"update_id": 10, "message": {"text": "x"}})  # не бросает
    assert alerts and alerts[0][0] == "doctor_update"


def test_safe_process_no_alert_when_processing_succeeds(monkeypatch):
    """Штатная обработка — алерта нет (иначе получим шум на каждый апдейт)."""
    alerts = []
    monkeypatch.setattr(poller, "alert_on_failure", lambda src, exc: alerts.append((src, exc)))
    monkeypatch.setattr(poller, "process_one", lambda u: None)
    poller._safe_process({"update_id": 12})
    assert alerts == []


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


# --- L1 (аудит логики, 2026-09-23, КРИТИЧНО): гейт на транспортном уровне ---
# Раньше fast_gate жил только в intake.handle_update ("doctor"-путь) — фото с
# подписью, тег анамнеза и сбой классификатора обходили детектор неотложки
# целиком. Теперь _check_emergency_gate() в process_one() видит КАЖДЫЙ текст
# ДО решения "куда" — эти тесты проверяют ровно три обходных пути из отчёта.

_EMERGENCY_TEXT = "грудь давит, отдаёт в левую руку, одышка"


@pytest.fixture()
def emergency_gate_env(monkeypatch):
    """Реальный fast_gate/handle_emergency (та же БД, что test_doctor_gate.py),
    Telegram и слой B замоканы — тест не должен бить по сети."""
    sent = []
    monkeypatch.setattr(poller.telegram, "send_message",
                        lambda chat_id, text, reply_to_message_id=None, parse_mode=None: (sent.append((chat_id, text)), 1)[1])
    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: LayerBResult(hit=False))
    yield sent


def test_emergency_gate_catches_photo_with_caption(monkeypatch, emergency_gate_env):
    """L1: фото+подпись раньше уходило в registrar.handle_update, минуя гейт —
    dispatch.route() отсекает фото ДО текстовой проверки (dispatch.py:164)."""
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: calls.setdefault("routed", "registrar") or "registrar")
    monkeypatch.setattr(poller.registrar, "handle_update", lambda update: calls.setdefault("registrar", True))

    update = {"update_id": 600, "message": {"chat": {"id": 8956401}, "message_id": 1,
              "photo": [{"file_id": "p"}], "caption": _EMERGENCY_TEXT}}
    poller.process_one(update)

    assert "registrar" not in calls  # маршрутизация не пошла вообще
    assert len(emergency_gate_env) == 1
    assert "скорую" in emergency_gate_env[0][1].lower()


def test_emergency_gate_catches_anamnesis_reply(monkeypatch, emergency_gate_env):
    """L1: текст с тегом анамнеза уходил в anamnesis.handle_reply, минуя гейт."""
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "anamnesis")
    monkeypatch.setattr(poller.anamnesis, "handle_reply", lambda update: calls.setdefault("anamnesis", True))

    update = {"update_id": 601, "message": {"chat": {"id": 8956401}, "message_id": 1, "text": _EMERGENCY_TEXT}}
    poller.process_one(update)

    assert "anamnesis" not in calls
    assert len(emergency_gate_env) == 1


def test_emergency_gate_catches_classifier_failure_fallback(monkeypatch, emergency_gate_env):
    """L1: сбой dispatch.route() (LLM-классификатор упал) ронял маршрут в
    "other" -> ingest_test_message — эмердженси-текст молча превращался в
    TEST-запись. Гейт теперь стоит ДО dispatch.route(), сбоя не видит вообще."""
    calls = {}

    def boom(update):
        raise RuntimeError("classify failed")

    monkeypatch.setattr(dispatch, "route", boom)
    monkeypatch.setattr(poller, "ingest_test_message", lambda update: calls.setdefault("ingested", True))

    update = {"update_id": 602, "message": {"chat": {"id": 8956401}, "message_id": 1, "text": _EMERGENCY_TEXT}}
    poller.process_one(update)

    assert "ingested" not in calls
    assert len(emergency_gate_env) == 1


def test_emergency_gate_no_op_for_safe_text(monkeypatch):
    """Регресс: обычный текст по-прежнему идёт в обычную маршрутизацию —
    гейт не должен глушить штатные сообщения."""
    calls = {}
    monkeypatch.setattr(dispatch, "route", lambda update: "doctor")
    monkeypatch.setattr(intake, "handle_update", lambda update: calls.setdefault("update", update))

    update = {"update_id": 603, "message": {"chat": {"id": 8956401}, "message_id": 1, "text": "болит голова"}}
    poller.process_one(update)

    assert calls.get("update") == update


def test_emergency_gate_no_text_skips_db(monkeypatch):
    """Голосовое/стикер без текста — гейт не должен даже открывать соединение
    с БД впустую (voice/document без caption — самый частый штатный случай)."""
    monkeypatch.setattr(poller, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("не должно было звать БД")))
    assert poller._check_emergency_gate({"message": {"voice": {"file_id": "v"}}}) is False


def test_emergency_gate_failure_falls_through_to_routing(monkeypatch):
    """Сбой самого гейта (БД недоступна и т.п.) не должен терять сообщение —
    пропускаем дальше в обычную маршрутизацию, а не глушим апдейт."""
    def boom_get_conn():
        raise RuntimeError("db down")

    monkeypatch.setattr(poller, "get_conn", boom_get_conn)
    assert poller._check_emergency_gate({"message": {"text": _EMERGENCY_TEXT}}) is False


# --- L4 (аудит логики, 2026-09-23, КРИТИЧНО): восстановление после рестарта -

def test_recover_pending_turns_notifies_and_clears(monkeypatch):
    """Прошлый процесс убит посреди хода (маркер остался) — при старте
    контейнера владелец получает видимое «потерялось, повтори», а не тишину."""
    from app.doctor import intake as intake_module

    pending_id = intake_module._mark_turn_pending("222", 9, 77, "текст потерянного хода")
    sent = []
    monkeypatch.setattr(poller.telegram, "send_message",
                        lambda chat_id, text, **k: sent.append((chat_id, text)))

    poller.recover_pending_turns()

    assert len(sent) == 1
    assert sent[0][0] == "222"
    assert "текст потерянного хода" in sent[0][1]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.doctor_pending_turn WHERE id = %s", (pending_id,))
        assert cur.fetchone()[0] == 0


def test_recover_pending_turns_no_op_when_empty(monkeypatch):
    """Штатный случай (чистое завершение всех прошлых ходов) — тишина, без
    единого сообщения владельцу."""
    sent = []
    monkeypatch.setattr(poller.telegram, "send_message", lambda chat_id, text, **k: sent.append((chat_id, text)))
    poller.recover_pending_turns()
    assert sent == []


def test_recover_pending_turns_notify_failure_still_clears(monkeypatch):
    """Сбой самой отправки уведомления не должен оставлять маркер висеть
    навсегда — иначе он спамил бы на каждый следующий рестарт."""
    from app.doctor import intake as intake_module

    pending_id = intake_module._mark_turn_pending("333", None, 88, "ещё один потерянный")

    def boom(chat_id, text, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(poller.telegram, "send_message", boom)
    poller.recover_pending_turns()  # не бросает

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.doctor_pending_turn WHERE id = %s", (pending_id,))
        assert cur.fetchone()[0] == 0


# --- Фаза 3: команда /tz (часовой пояс) --------------------------------------

def test_route_tz_command_is_deterministic():
    """«/tz ...» уходит в отдельную ветку — без LLM-классификатора (тест не
    ходит в OpenRouter, ответ детерминированный)."""
    from app.doctor import dispatch
    update = {"update_id": 900, "message": {"chat": {"id": 8956401}, "text": "/tz Asia/Bangkok"}}
    assert dispatch.route(update) == "tz_command"


def test_process_one_tz_command_calls_handler(monkeypatch):
    calls = {}
    monkeypatch.setattr(poller, "_handle_tz_command", lambda u: calls.setdefault("tz", u))
    poller.process_one({"update_id": 901, "message": {"chat": {"id": 8956401}, "text": "/tz Bangkok"}})
    assert "tz" in calls


def test_handle_tz_command_set_status_reset(monkeypatch):
    """Полный круг: смена зоны → статус → мусорный ввод отклонён → домой.
    Зона меняется в card_test (people-двойник conftest), восстанавливаем после."""
    sent = []
    monkeypatch.setattr(poller.telegram, "send_message",
                        lambda chat_id, text, **kw: sent.append(text))
    from app import people
    saved = people.get_person()["current_tz"]

    def upd(text):
        return {"update_id": 902, "message": {"chat": {"id": 8956401}, "text": text}}

    try:
        poller._handle_tz_command(upd("/tz Bangkok"))
        assert people.get_person()["current_tz"] == "Asia/Bangkok"
        assert "переключён" in sent[-1]

        poller._handle_tz_command(upd("/tz"))
        assert "Часовой пояс" in sent[-1] and "Asia/Bangkok" in sent[-1]

        poller._handle_tz_command(upd("/tz not-a-zone!!"))
        assert people.get_person()["current_tz"] == "Asia/Bangkok"  # мусор не применяется
        assert "Не понял" in sent[-1]

        poller._handle_tz_command(upd("/tz home"))
        assert people.get_person()["current_tz"] == saved
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {schema()}.people SET current_tz = %s WHERE id = 'self'", (saved,))
            conn.commit()
