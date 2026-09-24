"""app/food_diary_bot.py — проводка живого бота Food diary_v5 (2026-09-21).
Всё внешнее (Telegram/OpenRouter) замокано — эти тесты проверяют оркестрацию
(какие функции зовутся с какими аргументами), не реальные API. Sheets-дубль
снят 2026-09-23 (постепенный отказ от Sheets, категория A). Живая проверка
(реальный вызов бота/LLM) — отдельным шагом перед cutover."""
import pytest

from app import food_diary_bot as bot
from app.db import get_conn

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


# --- handle_update chat_id guard (2026-09-22, внешний аудит K5) -------------

def test_handle_update_ignores_foreign_chat_id_message(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "handle_message", lambda message: calls.append(message))
    monkeypatch.setattr(bot, "handle_callback", lambda cq: calls.append(cq))

    update = {"update_id": 1, "message": {"chat": {"id": 999999}, "text": "чужое сообщение"}}
    bot.handle_update(update)

    assert calls == []


def test_handle_update_ignores_foreign_chat_id_callback(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "handle_callback", lambda cq: calls.append(cq))

    update = {"update_id": 2, "callback_query": {"id": "cbq", "data": "confirm|1",
                                                  "message": {"chat": {"id": 999999}, "message_id": 1}}}
    bot.handle_update(update)

    assert calls == []


def test_handle_update_accepts_owner_chat_id(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "handle_message", lambda message: calls.append(message))

    update = {"update_id": 3, "message": {"chat": {"id": 8956401}, "text": "омлет"}}
    bot.handle_update(update)

    assert len(calls) == 1


# --- handle_callback ---------------------------------------------------------

def test_handle_callback_confirm_edits_message(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "answer_callback_query", lambda cb_id, text: calls.append(("answer", text)))
    monkeypatch.setattr(bot, "edit_message_text", lambda chat_id, msg_id, text: calls.append(("edit", text)))

    cq = {"id": "cbq1", "data": "confirm|123", "message": {"chat": {"id": 111}, "message_id": 222, "text": "✅ Записано! 🍽 Омлет\n[ID:123]"}}
    bot.handle_callback(cq)

    assert calls[0] == ("answer", "Принято")
    assert "✅ *Подтверждено*" in calls[1][1]


def test_handle_callback_delete_removes_from_pg(monkeypatch):
    """2026-09-23: Sheets-дубль снят (постепенный отказ от Sheets, категория A) —
    удаление теперь только в Postgres."""
    monkeypatch.setattr(bot, "answer_callback_query", lambda *a: None)
    edits = []
    monkeypatch.setattr(bot, "edit_message_text", lambda chat_id, msg_id, text: edits.append(text))
    deleted_pg = []
    monkeypatch.setattr("app.food_diary.delete_meal", lambda cur, entry_id: deleted_pg.append(entry_id))

    cq = {"id": "cbq2", "data": "delete|999", "message": {"chat": {"id": 111}, "message_id": 222, "text": "✅ Записано!"}}
    bot.handle_callback(cq)

    assert "❌ *Удалено*" in edits[0]
    assert deleted_pg == ["999"]


def test_handle_callback_edit_sends_force_reply_prompt(monkeypatch):
    monkeypatch.setattr(bot, "answer_callback_query", lambda *a: None)
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None, force_reply=False: sent.append((text, force_reply)))

    cq = {"id": "cbq3", "data": "edit|456", "message": {"chat": {"id": 111}, "message_id": 222, "text": "✅ Записано! 🍽 Омлет\n[ID:456]"}}
    bot.handle_callback(cq)

    assert "[ID:456]" in sent[0][0]
    assert sent[0][1] is True


# --- handle_message: команды --------------------------------------------------

def test_handle_message_command_sends_stats(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None: sent.append((chat_id, text, reply_markup)))
    monkeypatch.setattr("app.food_diary.build_stats_message", lambda meals, user, cmd: {"text": "статистика", "reply_markup": {"inline_keyboard": []}})

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "text": "/today"}
    bot.handle_message(msg)

    assert sent[0][0] == 111
    assert sent[0][1] == "статистика"


# --- handle_message: новая запись (текст) -------------------------------------

TEST_ENTRY_ID = "test-bot-88888888"


def test_handle_message_text_inserts_meal_and_sends_confirmation(monkeypatch):
    monkeypatch.setattr(bot, "call_text_llm", lambda prompt, timeout=30.0: '```json\n{"Meal_description": "Тест, 100г", "Calories": 300, "Proteins": 10, "Carbs": 20, "Fats": 5}\n```')
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None, force_reply=False: sent.append((chat_id, text, reply_markup)))

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "text": "омлет 100г", "message_id": 88888888, "date": 1758000000}
    bot.handle_message(msg)

    assert sent[0][0] == 111
    assert "Тест, 100г" in sent[0][1]
    assert "[ID:88888888]" in sent[0][1]  # ФИКС: тег всегда в подтверждении
    assert sent[0][2]["inline_keyboard"][0][0]["callback_data"] == "confirm|88888888"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Calories" FROM health.meals WHERE "Entry_ID" = %s', ("88888888",))
        assert cur.fetchone() == ("300",)


def test_handle_message_text_no_llm_response_sends_warning_no_write(monkeypatch):
    monkeypatch.setattr(bot, "call_text_llm", lambda prompt, timeout=30.0: "")
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None, force_reply=False: sent.append(text))

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "text": "омлет", "message_id": 88888888, "date": 1758000000}
    bot.handle_message(msg)

    assert "не ответила" in sent[0]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM health.meals WHERE "Entry_ID" = %s', ("88888888",))
        assert cur.fetchone() == (0,)


def test_handle_message_photo_downloads_and_calls_photo_llm(monkeypatch):
    downloaded = []
    monkeypatch.setattr(bot, "download_file", lambda file_id: downloaded.append(file_id) or b"fake-image-bytes")
    called_with = {}

    def fake_photo_llm(prompt, image_bytes, timeout=30.0):
        called_with["prompt"] = prompt
        called_with["bytes"] = image_bytes
        return '```json\n{"Meal_description": "Фото-блюдо, 150г", "Calories": 400, "Proteins": 20, "Carbs": 30, "Fats": 10}\n```'

    monkeypatch.setattr(bot, "call_photo_llm", fake_photo_llm)
    monkeypatch.setattr(bot, "send_message", lambda *a, **kw: None)

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "photo": [{"file_id": "small"}, {"file_id": "big"}],
           "message_id": 88888888, "date": 1758000000}
    bot.handle_message(msg)

    assert downloaded == ["big"]  # наибольшее фото — последнее в массиве
    assert called_with["bytes"] == b"fake-image-bytes"


# --- handle_message: правка ---------------------------------------------------

def test_handle_edit_reply_updates_existing_meal(monkeypatch):
    with get_conn() as conn, conn.cursor() as cur:
        from app import food_diary as fd
        fd.insert_meal(cur, "88888888", "Влад", "2026-09-21T12:00:00+10:00", {
            "Meal_description": "Старое", "Calories": "200", "Proteins": "10", "Carbs": "10", "Fats": "5",
            **{k: "0" for k in fd._INSERT_COLS[8:]},
        })
        conn.commit()

    monkeypatch.setattr(bot, "call_text_llm", lambda prompt, timeout=30.0: '```json\n{"Meal_description": "Новое, 200г", "Calories": 500, "Proteins": 25, "Carbs": 40, "Fats": 15}\n```')
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None, force_reply=False: sent.append(text))

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "text": "на самом деле было 200г",
           "reply_to_message": {"text": "🍽 Старое\n\n⚖️ Введите новые данные\n[ID:88888888]"}}
    bot.handle_message(msg)

    assert "Новое, 200г" in sent[0]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Calories" FROM health.meals WHERE "Entry_ID" = %s', ("88888888",))
        assert cur.fetchone() == ("500",)


def test_handle_edit_reply_no_id_in_reply_text_sends_warning(monkeypatch):
    monkeypatch.setattr(bot, "call_text_llm", lambda prompt, timeout=30.0: '```json\n{"Calories": 100}\n```')
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, reply_markup=None, force_reply=False: sent.append(text))

    msg = {"chat": {"id": 111}, "from": {"first_name": "Влад"}, "text": "правка",
           "reply_to_message": {"text": "✅ Записано! 🍽 Омлет (старый формат без тега)"}}
    bot.handle_message(msg)

    assert "не нашёл" in sent[0].lower()
