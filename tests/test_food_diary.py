"""app/food_diary.py — порт n8n Food diary_v5 (2026-09-21, 33 ноды, самый
сложный воркфлоу проекта). Юниты на чистую логику: классификация сообщений,
парсинг callback, промпты, разбор ответа LLM, статистика day/week, SQL для
health.meals. Живой Telegram-бот НЕ подключён (см. докстринг модуля) —
здесь только то, что тестируемо без него."""
from datetime import datetime

import pytest

from app import food_diary as fd
from app import timeutil
from app.db import get_conn


# --- классификация сообщений ------------------------------------------------

def test_classify_reply_takes_priority():
    msg = {"reply_to_message": {"text": "..."}, "text": "150г"}
    assert fd.classify_message(msg) == "reply"


def test_classify_command():
    assert fd.classify_message({"text": "/today"}) == "command"
    assert fd.classify_message({"text": "/week"}) == "command"


def test_classify_text_and_photo():
    assert fd.classify_message({"photo": [{}], "caption": "омлет"}) == "text_and_photo"
    assert fd.classify_message({"photo": [{}], "text": "омлет"}) == "text_and_photo"


def test_classify_text_only():
    assert fd.classify_message({"text": "овсянка 200г"}) == "text"


def test_classify_photo_only():
    assert fd.classify_message({"photo": [{}]}) == "photo"


def test_classify_reply_wins_over_photo_documented_deviation():
    # ОСОЗНАННОЕ отличие от оригинала (см. докстринг) — reply приоритетнее
    # content-type, а не оба выхода независимо, как было в n8n Switch
    msg = {"reply_to_message": {"text": "..."}, "photo": [{}], "caption": "исправление"}
    assert fd.classify_message(msg) == "reply"


def test_is_callback():
    assert fd.is_callback({"callback_query": {"data": "confirm|1"}}) is True
    assert fd.is_callback({"message": {"text": "hi"}}) is False


# --- callback-разбор ---------------------------------------------------------

def test_parse_callback_data_confirm():
    assert fd.parse_callback_data("confirm|12345") == ("confirm", "12345")


def test_parse_callback_data_delete():
    assert fd.parse_callback_data("delete|999") == ("delete", "999")


def test_parse_callback_data_empty():
    assert fd.parse_callback_data("") == (None, None)
    assert fd.parse_callback_data(None) == (None, None)


def test_parse_callback_data_no_row_id():
    assert fd.parse_callback_data("confirm") == ("confirm", None)


def test_build_answer_text():
    assert fd.build_answer_text("edit") == "Жду новые данные"
    assert fd.build_answer_text("confirm") == "Принято"
    assert fd.build_answer_text("delete") == "Принято"
    assert fd.build_answer_text(None) == "Принято"


# --- промпты ------------------------------------------------------------------

def test_build_text_prompt_includes_user_text():
    p = fd.build_text_prompt("овсянка с бананом")
    assert "овсянка с бананом" in p
    assert "JSON" in p


def test_build_photo_prompt_with_and_without_caption():
    with_caption = fd.build_photo_prompt("без сахара")
    without = fd.build_photo_prompt("")
    assert "без сахара" in with_caption
    assert "комментари" in with_caption
    assert "изображению" in without


def test_build_edit_prompt_includes_both_descriptions():
    p = fd.build_edit_prompt("Овсянка, 250г", "на самом деле 350г")
    assert "Овсянка, 250г" in p
    assert "350г" in p


def test_extract_edit_context_finds_id_and_description():
    reply_text = "🍽 Овсянка на молоке, 250г\n🔥 350 ккал\n\n⚖️ Введите новые данные\n[ID:98765]"
    entry_id, desc = fd.extract_edit_context(reply_text)
    assert entry_id == "98765"
    assert desc == "Овсянка на молоке, 250г"


def test_extract_edit_context_missing_id_returns_none():
    entry_id, desc = fd.extract_edit_context("✅ Записано! 🍽 Омлет, 150г\n🔥 200 ккал")
    assert entry_id is None
    assert desc == "Омлет, 150г"  # описание находится, ID — нет (реальный дефект оригинала)


def test_extract_edit_context_empty_text():
    entry_id, desc = fd.extract_edit_context("")
    assert entry_id is None
    assert desc == "неизвестно"


def test_extract_loose_entry_id_matches_bracket_tag():
    assert fd.extract_loose_entry_id("...\n[ID:555]") == "555"


def test_extract_loose_entry_id_matches_without_brackets():
    assert fd.extract_loose_entry_id("ID: 777 где-то в тексте") == "777"


def test_extract_loose_entry_id_none_when_absent():
    assert fd.extract_loose_entry_id("Записано! 🍽 Омлет") is None
    assert fd.extract_loose_entry_id(None) is None


# --- разбор ответа LLM ---------------------------------------------------------

def test_extract_llm_text_from_openrouter_response():
    resp = {"choices": [{"message": {"content": "```json\n{\"Calories\": 100}\n```"}}]}
    assert "Calories" in fd.extract_llm_text(resp)


def test_extract_llm_text_empty_choices():
    assert fd.extract_llm_text({"choices": []}) == ""
    assert fd.extract_llm_text({}) == ""


def test_parse_json_from_ai_extracts_from_markdown_block():
    raw = 'Вот результат:\n```json\n{"Meal_description": "Омлет, 150г", "Calories": 220}\n```\nПриятного аппетита!'
    parsed = fd.parse_json_from_ai(raw)
    assert parsed["Calories"] == 220
    assert parsed["Meal_description"] == "Омлет, 150г"


def test_parse_json_from_ai_raises_on_no_json():
    with pytest.raises(ValueError):
        fd.parse_json_from_ai("Извините, не могу определить блюдо.")


def test_parse_json_from_ai_raises_on_empty():
    with pytest.raises(ValueError):
        fd.parse_json_from_ai("")


def test_parse_json_from_ai_raises_on_malformed_json():
    with pytest.raises(Exception):
        fd.parse_json_from_ai("{not valid json}")


# --- normalize_food_group_tags (2026-09-22, слито из app/diet_tagger.py) -----
# Раньше это была отдельная разборка ОТДЕЛЬНОГО, до 15 мин позже сделанного
# LLM-вызова (diet_tagger.parse_tags, удалён). Теперь та же нормализация
# применяется к dict, который уже вернул parse_json_from_ai() в ОДНОМ ответе
# вместе с нутриентами — по запросу Влада: один вызов на приём пищи.

def test_normalize_food_group_tags_valid_response():
    parsed = {"NOVA": 2, "veg_g": 50, "fruit_g": 0, "wholegrain_g": 30,
              "legume_nut_g": 0, "redmeat_g": 0, "ssb_ml": 0, "pufa_g": 5, "plants": "морковь, лук"}
    tags = fd.normalize_food_group_tags(parsed)
    assert tags["NOVA"] == 2
    assert tags["veg_g"] == 50.0
    assert tags["ПНЖ"] == 5.0  # LLM-ключ pufa_g -> колонка ПНЖ
    assert tags["plants"] == "морковь, лук"


def test_normalize_food_group_tags_clamps_nova_to_1_4():
    assert fd.normalize_food_group_tags({"NOVA": 9})["NOVA"] == 4
    assert fd.normalize_food_group_tags({"NOVA": -3})["NOVA"] == 1


def test_normalize_food_group_tags_nova_missing_or_zero_falls_back_to_1():
    assert fd.normalize_food_group_tags({})["NOVA"] == 1
    assert fd.normalize_food_group_tags({"NOVA": 0})["NOVA"] == 1


def test_normalize_food_group_tags_missing_numeric_fields_default_to_zero():
    tags = fd.normalize_food_group_tags({"NOVA": 1})
    assert tags["veg_g"] == 0.0
    assert tags["ПНЖ"] == 0.0
    assert tags["plants"] == ""


def test_normalize_food_group_tags_plants_truncated_to_300_chars():
    long_plants = "растение, " * 50
    tags = fd.normalize_food_group_tags({"NOVA": 1, "plants": long_plants})
    assert len(tags["plants"]) <= 300


def test_normalize_food_group_tags_rounds_to_one_decimal():
    tags = fd.normalize_food_group_tags({"NOVA": 1, "veg_g": 33.333})
    assert tags["veg_g"] == 33.3


def test_parse_and_normalize_single_ai_response_covers_both_nutrients_and_food_group():
    """Инвариант слияния: один ответ модели -> и нутриенты, и классификация
    качества рациона, без второго LLM-вызова."""
    raw = ('```json\n{"Meal_description": "Гречка с курицей, 300г", "Calories": 450, '
           '"Proteins": 35, "NOVA": 1, "veg_g": 0, "plants": "гречка"}\n```')
    parsed = fd.parse_json_from_ai(raw)
    parsed.update(fd.normalize_food_group_tags(parsed))
    assert parsed["Calories"] == 450
    assert parsed["NOVA"] == 1
    assert parsed["plants"] == "гречка"
    assert parsed["ПНЖ"] == 0.0


# --- статистика day/week -----------------------------------------------------

def _meal(user, date_str, cal, prot, carb, fat):
    return {"User_ID": user, "Date": date_str, "Calories": cal, "Proteins": prot, "Carbs": carb, "Fats": fat}


def test_build_stats_message_today_sums_only_todays_meals():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timeutil.person_tz())
    meals = [
        _meal("Влад Васюк", "2026-09-21T08:00", "500", "30", "50", "20"),
        _meal("Влад Васюк", "2026-09-21T13:00", "700", "40", "60", "25"),
        _meal("Влад Васюк", "2026-09-20T08:00", "9999", "9", "9", "9"),  # вчера — не считается
        _meal("Другой Юзер", "2026-09-21T08:00", "9999", "9", "9", "9"),  # другой пользователь
    ]
    result = fd.build_stats_message(meals, "Влад Васюк", "/today", now)
    assert "1200" in result["text"]  # 500+700 калорий
    assert "9999" not in result["text"]


def test_build_stats_message_week_sums_last_7_days():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timeutil.person_tz())
    meals = [_meal("Влад Васюк", f"2026-09-{d:02d}T08:00", "1000", "10", "10", "10") for d in range(15, 22)]
    meals.append(_meal("Влад Васюк", "2026-09-14T08:00", "9999", "9", "9", "9"))  # 8 дней назад — не входит
    result = fd.build_stats_message(meals, "Влад Васюк", "/week", now)
    assert "7000" in result["text"]
    assert "9999" not in result["text"]


def test_build_stats_message_no_meals_found():
    result = fd.build_stats_message([], "Влад Васюк", "/today")
    assert "не найдено" in result["text"]


def test_build_stats_message_has_reply_buttons():
    result = fd.build_stats_message([], "Влад Васюк", "/today")
    buttons = result["reply_markup"]["inline_keyboard"][0]
    assert any(b["callback_data"] == "/today" for b in buttons)
    assert any(b["callback_data"] == "/week" for b in buttons)


def test_generate_bar_caps_segments_but_not_percentage_text():
    bar = fd.generate_bar(5000, 2400)
    assert "🟩" * 10 in bar  # сегментов максимум 10, дальше не растёт
    assert "208%" in bar  # но сам процент в тексте — реальный, не капается


def test_telegram_user_id_formats_name():
    assert fd.telegram_user_id({"first_name": "Влад", "last_name": "Васюк"}) == "Влад Васюк"
    assert fd.telegram_user_id({"first_name": "Влад"}) == "Влад"
    assert fd.telegram_user_id({"first_name": "Влад", "last_name": None}) == "Влад"


# --- SQL для health.meals (реальная таблица, тестовые Entry_ID, cleanup) ------

TEST_ENTRY_ID = "test-fd-99999999"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        conn.commit()


def _nutrients(calories="500"):
    return {
        "Meal_description": "Тестовое блюдо, 100г", "Calories": calories, "Proteins": "20",
        "Carbs": "50", "Fats": "10", "Магний": "0", "Витамин D": "0", "Омега-3 (EPA/DHA)": "0",
        "Селен": "0", "Йод": "0", "Калий": "0", "Железо": "0", "Кальций": "0", "Витамин B12": "0",
        "Витамин К": "0", "Витамин Е": "0", "Цинк": "0", "Клетчатка": "0", "Холестерин": "0",
        "Добавленный сахар": "0", "Натрий": "0", "Кофеин": "0", "Алкоголь": "0",
        "Трансжиры": "0", "Насыщенные жиры": "0",
    }


def test_insert_meal_then_reinsert_same_entry_id_upserts_not_duplicates():
    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, TEST_ENTRY_ID, "Влад Васюк", "2026-09-21T12:00:00+10:00", _nutrients("500"))
        conn.commit()
        cur.execute('SELECT "Calories" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        assert cur.fetchone() == ("500",)

    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, TEST_ENTRY_ID, "Влад Васюк", "2026-09-21T12:00:00+10:00", _nutrients("777"))
        conn.commit()
        cur.execute('SELECT count(*), "Calories" FROM health.meals WHERE "Entry_ID" = %s GROUP BY "Calories"', (TEST_ENTRY_ID,))
        assert cur.fetchone() == (1, "777")


def test_update_meal_changes_existing_row():
    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, TEST_ENTRY_ID, "Влад Васюк", "2026-09-21T12:00:00+10:00", _nutrients("500"))
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        fd.update_meal(cur, TEST_ENTRY_ID, "Влад Васюк", _nutrients("650"))
        conn.commit()
        cur.execute('SELECT "Calories" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        assert cur.fetchone() == ("650",)


def test_delete_meal_removes_row():
    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, TEST_ENTRY_ID, "Влад Васюк", "2026-09-21T12:00:00+10:00", _nutrients())
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        fd.delete_meal(cur, TEST_ENTRY_ID)
        conn.commit()
        cur.execute('SELECT count(*) FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        assert cur.fetchone() == (0,)


def test_delete_meal_nonexistent_id_no_crash():
    with get_conn() as conn, conn.cursor() as cur:
        fd.delete_meal(cur, "does-not-exist-12345")
        conn.commit()


def test_insert_meal_writes_food_group_columns_from_same_insert():
    """2026-09-22: NOVA/veg_g/... раньше дописывались ОТДЕЛЬНЫМ UPDATE'ом
    (diet_tagger, до 15 мин позже) — теперь тем же INSERT, что нутриенты."""
    nutrients = _nutrients("500")
    nutrients.update(fd.normalize_food_group_tags(
        {"NOVA": 3, "veg_g": 20, "pufa_g": 1.5, "plants": "лук, морковь"}))
    with get_conn() as conn, conn.cursor() as cur:
        fd.insert_meal(cur, TEST_ENTRY_ID, "Влад Васюк", "2026-09-21T12:00:00+10:00", nutrients)
        conn.commit()
        cur.execute('SELECT "NOVA", "veg_g", "ПНЖ", "plants" FROM health.meals WHERE "Entry_ID" = %s', (TEST_ENTRY_ID,))
        # text-колонки: psycopg передаёт Python int/float как есть, Postgres сам
        # кастует в text (20.0 -> '20', как и остальные нутриенты в этой функции,
        # не str()-принудительно, как раньше делал отдельный diet_tagger.write_tags).
        assert cur.fetchone() == ("3", "20", "1.5", "лук, морковь")
