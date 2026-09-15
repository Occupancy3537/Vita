"""Step 2 плана нового доктора (§3.2) — dispatch.py: card-service решает
"доктору или нет" само, без n8n. classify_category делает реальный вызов
OpenRouter — мокается везде, кроме отдельного явного live-теста."""
import pytest

from app.doctor import dispatch


def _update(text=None, caption=None, photo=None, document=None, reply_text=None, update_id=1):
    msg = {"message_id": 1, "chat": {"id": 123}}
    if text is not None:
        msg["text"] = text
    if caption is not None:
        msg["caption"] = caption
    if photo is not None:
        msg["photo"] = photo
    if document is not None:
        msg["document"] = document
    if reply_text is not None:
        msg["reply_to_message"] = {"message_id": 2, "text": reply_text}
    return {"update_id": update_id, "message": msg}


def test_photo_always_other_no_llm_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для фото")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(caption="что это на фото", photo=[{"file_id": "x"}])) == "other"


def test_document_always_other_no_llm_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для документа")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(document={"file_id": "d1"})) == "other"


def test_anamnesis_reply_always_other_no_llm_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для ответа на анамнез")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    r = dispatch.route(_update(text="да, было", reply_text="Как дела со сном? #A03"))
    assert r == "other"


def test_is_anamnesis_reply_detects_tag():
    assert dispatch.is_anamnesis_reply(_update(text="x", reply_text="вопрос #A12")) is True
    assert dispatch.is_anamnesis_reply(_update(text="x", reply_text="обычная реплика")) is False
    assert dispatch.is_anamnesis_reply(_update(text="x")) is False


def test_voice_or_no_text_defaults_to_doctor(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор без текста")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 123}, "voice": {"file_id": "v1"}}}
    assert dispatch.route(update) == "doctor"


def test_symptom_category_routes_to_doctor(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image: "SYMPTOM")
    assert dispatch.route(_update(text="болит голова")) == "doctor"


def test_calendar_and_sleep_route_to_doctor(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image: "CALENDAR")
    assert dispatch.route(_update(text="когда приём у врача")) == "doctor"
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image: "SLEEP")
    assert dispatch.route(_update(text="как спал")) == "doctor"


def test_test_category_routes_to_other(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image: "TEST")
    assert dispatch.route(_update(text="запиши холестерин 5.5 ммоль/л")) == "other"


def test_classify_category_unparseable_response_defaults_to_symptom(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "непонятно что"}}]}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    assert dispatch.classify_category("текст", has_image=False) == "SYMPTOM"
