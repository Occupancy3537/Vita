"""Step 2 плана нового доктора (§3.2) — dispatch.py: card-service решает
"доктору или нет" само, без n8n. classify_category делает реальный вызов
OpenRouter — мокается везде, кроме отдельного явного live-теста."""
import pytest

from app.doctor import dispatch


def _update(text=None, caption=None, photo=None, document=None, reply_text=None, update_id=1,
            reply_from_bot=False):
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
        msg["reply_to_message"] = {"message_id": 2, "text": reply_text,
                                   "from": {"is_bot": reply_from_bot, "username": "bot"}}
    return {"update_id": update_id, "message": msg}


def test_photo_always_registrar_no_llm_call(monkeypatch):
    # Волна 3 (B2, 2026-09-18): фото -> регистратор (детерминированно, без LLM).
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для фото")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(caption="что это на фото", photo=[{"file_id": "x"}])) == "registrar"


def test_document_always_registrar_no_llm_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для документа")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(document={"file_id": "d1"})) == "registrar"


def test_anamnesis_reply_deterministic_no_llm_call(monkeypatch):
    # Волна 2 (B1, 2026-09-17): реплай на анамнез идёт в "anamnesis" (обрабатывает
    # card-service сам), по-прежнему ДЕТЕРМИНИРОВАННО — классификатор не зовётся.
    def boom(*a, **k):
        raise AssertionError("не должен звать классификатор для ответа на анамнез")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    r = dispatch.route(_update(text="да, было", reply_text="Как дела со сном? #A03"))
    assert r == "anamnesis"


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
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image, context_hint="": "SYMPTOM")
    assert dispatch.route(_update(text="болит голова")) == "doctor"


def test_calendar_and_sleep_route_to_doctor(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image, context_hint="": "CALENDAR")
    assert dispatch.route(_update(text="когда приём у врача")) == "doctor"
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image, context_hint="": "SLEEP")
    assert dispatch.route(_update(text="как спал")) == "doctor"


def test_test_category_routes_to_other(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda text, has_image, context_hint="": "TEST")
    assert dispatch.route(_update(text="запиши холестерин 5.5 ммоль/л")) == "other"


def test_classify_category_unparseable_response_defaults_to_symptom(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "непонятно что"}}]}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    assert dispatch.classify_category("текст", has_image=False) == "SYMPTOM"


# --- F5 (внешний аудит логики, 2026-09-22): разбор ответа классификатора ---

class _Resp:
    def __init__(self, content):
        self._c = content
    def raise_for_status(self): pass
    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


@pytest.mark.parametrize("content,expected", [
    ("SYMPTOM", "SYMPTOM"),
    ("TEST", "TEST"),
    ("CALENDAR", "CALENDAR"),
    ("SLEEP", "SLEEP"),
    ("Это SYMPTOM, не TEST", "SYMPTOM"),    # регресс F5: раньше подстрочный матч давал TEST
    ("NOT_TEST", "SYMPTOM"),                 # подстрока не считается словом
    ("TEST (не SYMPTOM)", "SYMPTOM"),        # при неоднозначности — осторожный SYMPTOM
    ("мусор без категорий", "SYMPTOM"),      # фолбэк «при сомнении», как в промпте
])
def test_parse_category_priority_and_fallback(content, expected):
    assert dispatch.parse_category(content) == expected


def test_classify_category_negated_test_is_not_test(monkeypatch):
    """Сквозь classify_category: ответ «это SYMPTOM, а не TEST» больше не уводит
    сообщение в маршрут данных (F5)."""
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp("Это SYMPTOM, а не TEST"))
    assert dispatch.classify_category("грудь давит", has_image=False) == "SYMPTOM"


# --- F10 (внешний аудит логики, 2026-09-22): тег анамнеза в тексте ---

def test_anamnesis_tag_in_own_text_routes_to_anamnesis(monkeypatch):
    """Текст вопроса обещает «или просто напиши ответ — я пойму по тегу #A05»:
    теперь тег в САМОМ тексте (без реплая) маршрутизируется в анамнез, мимо LLM."""
    def boom(*a, **k):
        raise AssertionError("тег в тексте не должен идти в классификатор")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(text="#A05 мне 44 года")) == "anamnesis"


def test_reply_to_bot_sticky_doctor_no_llm_call(monkeypatch):
    # Инцидент 18.09 09:05 VL: короткий ответ потерялся в "other". Порт Capitan
    # Sticky Route: реплай на сообщение бота = продолжение разговора с доктором.
    def boom(*a, **k):
        raise AssertionError("реплай на бота не должен идти в классификатор")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(text="нет, красных флагов никогда не было",
                                  reply_text="1. Нет ли онемения в промежности?", reply_from_bot=True)) == "doctor"


def test_reply_to_bot_slash_not_sticky(monkeypatch):
    seen = {}
    def fake(text, has_image, context_hint=""):
        seen["text"] = text
        return "TEST"
    monkeypatch.setattr(dispatch, "classify_category", fake)
    monkeypatch.setattr(dispatch, "recent_bot_question", lambda cid: "")
    assert dispatch.route(_update(text="/start", reply_text="вопрос?", reply_from_bot=True)) == "other"
    assert seen["text"] == "/start"


def test_reply_to_human_not_sticky(monkeypatch):
    monkeypatch.setattr(dispatch, "classify_category", lambda *a, **k: "SYMPTOM")
    monkeypatch.setattr(dispatch, "recent_bot_question", lambda cid: "")
    assert dispatch.route(_update(text="спасибо", reply_text="сообщение человека",
                                  reply_from_bot=False)) == "doctor"


def test_anamnesis_reply_wins_over_sticky(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("анамнез-реплай не должен идти в классификатор")
    monkeypatch.setattr(dispatch, "classify_category", boom)
    assert dispatch.route(_update(text="ответ", reply_text="🧬 Анамнез 11/30\n#A07",
                                  reply_from_bot=True)) == "anamnesis"


def test_context_hint_passed_to_classifier(monkeypatch):
    # Голое (не-реплай) короткое сообщение после вопроса доктора: классификатор
    # получает контекст последнего вопроса и должен увидеть в этом SYMPTOM.
    got = {}
    def fake(text, has_image, context_hint=""):
        got["hint"] = context_hint
        return "SYMPTOM" if context_hint else "TEST"
    monkeypatch.setattr(dispatch, "classify_category", fake)
    monkeypatch.setattr(dispatch, "recent_bot_question", lambda cid: "Нет ли онемения в промежности?")
    assert dispatch.route(_update(text="нет, красных флагов никогда не было")) == "doctor"
    assert "онемения" in got["hint"]
