"""Phase 1 плана нового доктора — intake.py: разбор Telegram update (чистая
функция) и handle_update целиком (приём + идемпотентность + диалоговая память +
Telegram round-trip). Telegram API, слой B (redflag_b.classify, вызывается
gate.slow_gate_followup после ответа) и агентный цикл (loop.run_turn, Phase 4 —
настоящий вызов OpenRouter) мокаются — юнит-тесты не должны бить по сети ни к
Telegram, ни к OpenRouter (единственный тест с настоящим вызовом LLM в проекте —
test_extraction_live.py, остальные мокают, см. её докстринг)."""
import threading
import time

from app.db import get_conn
from app.doctor import gate, intake as intake_module, loop, telegram as telegram_module
from app.doctor.contract import TurnResult
from app.doctor.dialog import recent_turns
from app.doctor.intake import handle_update, parse_update
from app.redflag_b import LayerBResult


def _text_update(update_id=1, chat_id=123, text="болит голова", message_id=10, reply_to=None):
    msg = {"message_id": message_id, "chat": {"id": chat_id}, "text": text}
    if reply_to:
        msg["reply_to_message"] = reply_to
    return {"update_id": update_id, "message": msg}


def test_parse_update_text():
    msg = parse_update(_text_update())
    assert msg is not None
    assert msg.chat_id == "123"
    assert msg.update_id == 1
    assert msg.text == "болит голова"
    assert msg.kind == "text"
    assert msg.message_id == 10


def test_parse_update_no_message_returns_none():
    assert parse_update({"update_id": 1, "my_chat_member": {}}) is None


def test_parse_update_photo():
    update = {
        "update_id": 2,
        "message": {
            "message_id": 11, "chat": {"id": 123}, "caption": "анализ крови",
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
        },
    }
    msg = parse_update(update)
    assert msg.kind == "photo"
    assert msg.photo_file_ids == ["small", "big"]
    assert msg.text == "анализ крови"


def test_parse_update_voice():
    update = {"update_id": 3, "message": {"message_id": 12, "chat": {"id": 123},
                                           "voice": {"file_id": "v1"}}}
    msg = parse_update(update)
    assert msg.kind == "voice"
    assert msg.voice_file_id == "v1"


def test_parse_update_document():
    update = {"update_id": 4, "message": {"message_id": 13, "chat": {"id": 123},
                                           "document": {"file_id": "d1"}}}
    msg = parse_update(update)
    assert msg.kind == "document"
    assert msg.document_file_id == "d1"


def test_parse_update_unknown_kind_when_no_text_no_media():
    update = {"update_id": 5, "message": {"message_id": 14, "chat": {"id": 123}, "sticker": {}}}
    msg = parse_update(update)
    assert msg.kind == "unknown"


def test_parse_update_sym_tag_extracted_from_reply():
    reply_to = {"message_id": 9, "text": "Как рука сегодня? #SYM:bol-kist-posle-broskov"}
    msg = parse_update(_text_update(text="лучше", reply_to=reply_to))
    assert msg.reply_symptom_id == "bol-kist-posle-broskov"
    assert msg.reply_to_message_id == 9


def test_parse_update_no_sym_tag_when_reply_has_none():
    reply_to = {"message_id": 9, "text": "обычная реплика без тега"}
    msg = parse_update(_text_update(reply_to=reply_to))
    assert msg.reply_symptom_id is None


def test_handle_update_writes_both_turns_and_replies_via_telegram(monkeypatch):
    sent = {}

    def fake_send_chat_action(chat_id, action="typing"):
        sent["typing"] = (chat_id, action)

    def fake_send_message(chat_id, text, reply_to_message_id=None, parse_mode=None):
        sent["placeholder"] = (chat_id, text, reply_to_message_id)
        return 999

    def fake_edit_message(chat_id, message_id, text, parse_mode=None):
        sent["final"] = (chat_id, message_id, text)

    monkeypatch.setattr(telegram_module, "send_chat_action", fake_send_chat_action)
    monkeypatch.setattr(telegram_module, "send_message", fake_send_message)
    monkeypatch.setattr(telegram_module, "edit_message", fake_edit_message)
    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: LayerBResult(hit=False))
    monkeypatch.setattr(loop, "run_turn", lambda **kw: TurnResult(
        turn_id=kw["turn_id"], reply_text="ответ про колет в боку (замокан цикл)"))

    handle_update(_text_update(update_id=100, chat_id=456, text="колет в боку", message_id=5))
    intake_module.flush()  # F9: длинная часть хода — в фоновом воркере

    assert sent["typing"] == ("456", "typing")
    assert sent["placeholder"] == ("456", "…", 5)
    assert sent["final"][0] == "456"
    assert sent["final"][1] == 999
    assert "колет в боку" in sent["final"][2]

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "456")
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert turns[0]["text"] == "колет в боку"
    assert turns[1]["role"] == "assistant"
    assert turns[1]["text"] == sent["final"][2]


def test_handle_update_duplicate_update_id_processed_once(monkeypatch):
    calls = {"n": 0}

    def fake_send_chat_action(chat_id, action="typing"):
        pass

    def fake_send_message(chat_id, text, reply_to_message_id=None, parse_mode=None):
        calls["n"] += 1
        return 1000

    def fake_edit_message(chat_id, message_id, text, parse_mode=None):
        pass

    monkeypatch.setattr(telegram_module, "send_chat_action", fake_send_chat_action)
    monkeypatch.setattr(telegram_module, "send_message", fake_send_message)
    monkeypatch.setattr(telegram_module, "edit_message", fake_edit_message)
    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: LayerBResult(hit=False))
    monkeypatch.setattr(loop, "run_turn", lambda **kw: TurnResult(turn_id=kw["turn_id"], reply_text="ок"))

    update = _text_update(update_id=200, chat_id=789, text="повтор")
    handle_update(update)
    handle_update(update)  # Телеграм переотправил тот же update_id
    intake_module.flush()

    assert calls["n"] == 1  # второй раз даже не дошли до отправки

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "789")
    assert len(turns) == 2  # user + assistant, не 4


def test_handle_update_l3_short_circuits_before_placeholder(monkeypatch):
    """Гейт (Phase 2) встроен в handle_update: L3 — короткое замыкание, плейсхолдер
    и заглушка Phase 1 не должны появляться вообще, эмердженси-ответ уходит одним
    sendMessage (без edit — цикл send-placeholder/edit не запускается)."""
    sent = []

    def fake_send_message(chat_id, text, reply_to_message_id=None, parse_mode=None):
        sent.append(text)
        return 1

    def fail_on_call(*args, **kwargs):
        raise AssertionError("не должно было вызываться на L3-пути")

    monkeypatch.setattr(telegram_module, "send_message", fake_send_message)
    monkeypatch.setattr(telegram_module, "send_chat_action", fail_on_call)
    monkeypatch.setattr(telegram_module, "edit_message", fail_on_call)
    monkeypatch.setattr(gate, "classify_layer_b", lambda text, prior_replies=None: LayerBResult(hit=False))

    handle_update(_text_update(update_id=300, chat_id=321,
                                text="грудь давит, отдаёт в левую руку, одышка", message_id=1))

    assert len(sent) == 1
    assert "скорую" in sent[0].lower()

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "321")
    assert len(turns) == 2
    assert turns[1]["role"] == "assistant"
    assert turns[1]["rf_level"] == "L3"
    assert turns[1]["wrote_anything"] is True


# --- F1 (внешний аудит логики, 2026-09-22): эмердженси-доставка с ретраями ---

def _emergency_msg(text="грудь давит, отдаёт в левую руку, одышка", chat_id=777, update_id=500):
    return parse_update(_text_update(update_id=update_id, chat_id=chat_id, text=text))


def test_emergency_delivery_retries_then_succeeds(monkeypatch):
    """Две попытки ботом доктора падают — третья доставляет; Hermes не нужен."""
    attempts = {"n": 0}
    sent = []

    def flaky(chat_id, text, reply_to_message_id=None, parse_mode=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("telegram 429")
        sent.append((chat_id, text))
        return 42

    hermes_calls = []
    monkeypatch.setattr(telegram_module, "send_message", flaky)
    monkeypatch.setattr(intake_module, "EMERGENCY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(intake_module.hermes_telegram, "send_message",
                        lambda *a, **k: hermes_calls.append(a))

    assert intake_module._deliver_emergency(_emergency_msg(), "🚨 ответ") is True
    assert attempts["n"] == 3
    assert sent == [("777", "🚨 ответ")]
    assert hermes_calls == []  # успех ботом доктора — фолбэк не нужен


def test_emergency_delivery_falls_back_to_hermes(monkeypatch):
    """Все попытки бота доктора падают — фолбэк Hermes-ботом в тот же чат."""
    def doctor_boom(chat_id, text, reply_to_message_id=None, parse_mode=None):
        raise RuntimeError("bot blocked")

    hermes_sent = []
    monkeypatch.setattr(telegram_module, "send_message", doctor_boom)
    monkeypatch.setattr(intake_module, "EMERGENCY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(intake_module.hermes_telegram, "send_message",
                        lambda chat_id, text, *a, **k: hermes_sent.append((chat_id, text)))

    assert intake_module._deliver_emergency(_emergency_msg(update_id=501), "🚨 ответ") is True
    assert hermes_sent == [("777", "🚨 ответ")]


def test_emergency_delivery_total_failure_never_raises(monkeypatch):
    """Оба канала недоступны — False и никакого исключения (обработку ронять нельзя)."""
    def boom(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram_module, "send_message", boom)
    monkeypatch.setattr(intake_module.hermes_telegram, "send_message", boom)
    monkeypatch.setattr(intake_module, "EMERGENCY_RETRY_DELAY_SECONDS", 0)

    assert intake_module._deliver_emergency(_emergency_msg(update_id=502), "🚨 ответ") is False


def test_handle_update_l3_delivery_failure_still_runs_layer_b(monkeypatch):
    """F1: даже когда ни один канал не доставил эмердженси, слой B всё равно
    считается (дописывает rf_event в ту же сессию) — раньше сбой отправки
    обрывал handle_update до slow_gate_followup."""
    def boom(*a, **k):
        raise RuntimeError("telegram down")

    layer_b_calls = []
    monkeypatch.setattr(telegram_module, "send_message", boom)
    monkeypatch.setattr(intake_module.hermes_telegram, "send_message", boom)
    monkeypatch.setattr(intake_module, "EMERGENCY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(gate, "slow_gate_followup", lambda text, source_id=None: layer_b_calls.append(text))

    handle_update(_text_update(update_id=600, chat_id=888,
                                text="грудь давит, отдаёт в левую руку, одышка"))

    assert layer_b_calls == ["грудь давит, отдаёт в левую руку, одышка"]


# --- F9 (внешний аудит логики, 2026-09-22): длинный ход не блокирует приём ---

def _quiet_telegram(monkeypatch, sent_list=None):
    """Тихая отправка: аккуратно мокает Telegram и слой B, собирает sent."""
    sent = sent_list if sent_list is not None else []
    monkeypatch.setattr(telegram_module, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_module, "send_message",
                        lambda chat_id, text, **k: (sent.append(text), 42)[1])
    monkeypatch.setattr(telegram_module, "edit_message", lambda *a, **k: None)
    monkeypatch.setattr(gate, "classify_layer_b", lambda *a, **k: LayerBResult(hit=False))
    return sent


def test_slow_turn_does_not_block_handle_update(monkeypatch):
    """F9: пока доктор «думает» (ход висит в воркере), handle_update уже
    вернулся — приём следующего сообщения возможен немедленно."""
    release = threading.Event()
    started = threading.Event()

    def slow_turn(**kw):
        started.set()
        release.wait(timeout=10)
        return TurnResult(turn_id=kw["turn_id"], reply_text="ответ (медленный)")

    monkeypatch.setattr(loop, "run_turn", slow_turn)
    _quiet_telegram(monkeypatch)

    t0 = time.monotonic()
    handle_update(_text_update(update_id=1000, chat_id=901, text="колет в боку"))
    dt = time.monotonic() - t0
    try:
        assert dt < 3.0, "handle_update ждал агентный цикл (%.1fс) — F9 не работает" % dt
        assert started.wait(timeout=5), "воркер не начал ход"
    finally:
        release.set()
        intake_module.flush()


def test_l3_emergency_not_blocked_by_busy_worker(monkeypatch):
    """F9, главное: пока предыдущий ход висит в воркере, следующее L3-сообщение
    получает эмердженси-ответ немедленно (раньше ждало до конца разбора)."""
    release = threading.Event()

    def slow_turn(**kw):
        release.wait(timeout=10)
        return TurnResult(turn_id=kw["turn_id"], reply_text="обычный ответ")

    monkeypatch.setattr(loop, "run_turn", slow_turn)
    sent = _quiet_telegram(monkeypatch)

    handle_update(_text_update(update_id=1001, chat_id=902, text="колет в боку"))
    t0 = time.monotonic()
    handle_update(_text_update(update_id=1002, chat_id=902,
                                text="грудь давит, отдаёт в левую руку, одышка"))
    dt = time.monotonic() - t0
    try:
        assert dt < 3.0, "L3 ждал занятого воркера (%.1fс)" % dt
        assert any("скорую" in s.lower() for s in sent), sent
    finally:
        release.set()
        intake_module.flush()


def test_finish_turn_failure_alerts_owner(monkeypatch):
    """Сбой внутри длинной части хода поллеру уже не виден (_safe_process его
    не поймает) — уходит алертом владельцу явно."""
    alerts = []
    from app import scheduler_alert
    monkeypatch.setattr(scheduler_alert, "alert_on_failure",
                        lambda src, exc: alerts.append((src, exc)))
    monkeypatch.setattr(loop, "run_turn", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    _quiet_telegram(monkeypatch)

    handle_update(_text_update(update_id=1003, chat_id=903, text="колет в боку"))
    intake_module.flush()
    assert alerts and alerts[0][0] == "doctor_turn"
