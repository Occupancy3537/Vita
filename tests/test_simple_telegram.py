"""«Стоп-кровь каналов» (2026-09-26, часть 1.2) — app/simple_telegram.py: до
этого лимит Telegram (4096 знаков) нигде не учитывался, отправка длиннее
падала целиком (реальный дайджест 25.09 был 3280/4096 — на грани). Юниты на
_split_text() (чистая функция) + send_message() (httpx мокается)."""
from unittest.mock import patch

import httpx

from app import simple_telegram as st


def _resp(ok=True):
    return httpx.Response(request=httpx.Request("POST", "http://test/"), status_code=200, json={"ok": ok})


def test_split_text_short_text_stays_one_chunk():
    assert st._split_text("короткий текст") == ["короткий текст"]


def test_split_text_exact_limit_stays_one_chunk():
    text = "a" * st.TELEGRAM_MESSAGE_LIMIT
    assert st._split_text(text) == [text]


def test_split_text_prefers_paragraph_boundary():
    """Режет по ПОСЛЕДНЕЙ пустой строке в пределах лимита — так секции остаются
    целыми и чанк заполняется максимально, а не режется по первой попавшейся."""
    part1 = "a" * 3000
    part2 = "b" * 2000
    text = part1 + "\n\n" + part2
    chunks = st._split_text(text, limit=4096)
    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2


def test_split_text_falls_back_to_single_newline():
    part1 = "x" * 4090
    part2 = "y" * 100
    text = part1 + "\n" + part2  # нет двойного перевода — режем по одиночному
    chunks = st._split_text(text, limit=4096)
    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2


def test_split_text_hard_cuts_when_paragraph_itself_too_long():
    text = "z" * 9000  # один "абзац" без единого переноса строки
    chunks = st._split_text(text, limit=4096)
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert all(len(c) <= 4096 for c in chunks)


def test_split_text_never_loses_the_tail():
    text = "\n\n".join(f"секция {i} " + "x" * 500 for i in range(20))
    chunks = st._split_text(text, limit=4096)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert all(len(c) <= 4096 for c in chunks)


def test_send_message_splits_long_text_into_several_calls():
    long_text = "\n\n".join(f"блок {i} " + "x" * 500 for i in range(20))
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["text"])
        return _resp(ok=True)

    with patch("app.simple_telegram.httpx.post", side_effect=fake_post):
        st.send_message("tok", "123", long_text)

    assert len(calls) > 1
    assert all(len(c) <= st.TELEGRAM_MESSAGE_LIMIT for c in calls)
    assert "".join(calls).replace("\n", "") == long_text.replace("\n", "")


def test_send_message_short_text_is_a_single_call():
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["text"])
        return _resp(ok=True)

    with patch("app.simple_telegram.httpx.post", side_effect=fake_post):
        st.send_message("tok", "123", "короткий текст")

    assert calls == ["короткий текст"]


def test_send_message_raises_on_first_failed_part_and_stops():
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["text"])
        return _resp(ok=(len(calls) == 1))  # первая часть ок, вторая — падает

    long_text = "\n\n".join(f"блок {i} " + "x" * 500 for i in range(20))
    with patch("app.simple_telegram.httpx.post", side_effect=fake_post):
        try:
            st.send_message("tok", "123", long_text)
            assert False, "должно было упасть на второй части"
        except RuntimeError:
            pass
    assert len(calls) == 2  # не пытается слать все части после первой неудачи
