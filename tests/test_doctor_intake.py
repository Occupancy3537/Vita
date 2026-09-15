"""Phase 1 плана нового доктора — intake.py: разбор Telegram update (чистая
функция) и handle_update целиком (приём + идемпотентность + диалоговая память +
Telegram round-trip). Telegram API, слой B (redflag_b.classify, вызывается
gate.slow_gate_followup после ответа) и агентный цикл (loop.run_turn, Phase 4 —
настоящий вызов OpenRouter) мокаются — юнит-тесты не должны бить по сети ни к
Telegram, ни к OpenRouter (единственный тест с настоящим вызовом LLM в проекте —
test_extraction_live.py, остальные мокают, см. её докстринг)."""
from app.db import get_conn
from app.doctor import gate, loop, telegram as telegram_module
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
