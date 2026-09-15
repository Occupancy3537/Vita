"""Phase 4 плана нового доктора — render.py: Telegram-HTML санитайзер. Тест на
реальный баг: Влад увидел `<b>...</b>` буквально в чате (скриншот 2026-09-15,
после первой живой проверки Phase 4) — parse_mode нигде не проставлялся,
санитайзера не было вообще. Не гипотетический случай, воспроизведён 1:1."""
from app.doctor.render import sanitize_for_telegram


def test_real_bug_bold_tags_pass_through_unchanged_when_valid():
    """Сам баг был не в разметке модели (она валидна), а в том, что intake.py
    отправлял текст без parse_mode="HTML" — санитайзер тут ничего не чинит по
    содержанию, просто подтверждаем, что валидные теги не ломаются по пути."""
    text = ("Положительная динамика — хороший признак.\n\n<b>Гипотезы:</b>\n"
            "1. <b>Микротравматизация</b> вследствие нагрузки.")
    out = sanitize_for_telegram(text)
    assert out == text


def test_markdown_bold_converted_to_html():
    assert sanitize_for_telegram("это **важно** знать") == "это <b>важно</b> знать"


def test_stray_asterisks_stripped():
    assert "*" not in sanitize_for_telegram("* пункт списка * ещё *текст*")


def test_br_and_p_become_newlines():
    assert sanitize_for_telegram("строка1<br>строка2") == "строка1\nстрока2"
    assert sanitize_for_telegram("<p>абзац1</p><p>абзац2</p>").count("\n\n") >= 1


def test_disallowed_tags_stripped_but_allowed_kept():
    out = sanitize_for_telegram("<div><b>жирный</b><script>alert(1)</script></div>")
    assert "<b>жирный</b>" in out
    assert "<div>" not in out
    assert "<script>" not in out


def test_unclosed_tag_autobalanced():
    """План: незакрытый тег роняет ВЕСЬ ответ у Telegram (400), не только вид —
    это функциональный тест, не косметический."""
    out = sanitize_for_telegram("начал <b>жирный текст без закрытия")
    assert out.count("<b>") == 1
    assert out.endswith("</b>")


def test_extra_closing_tag_ignored_not_crash():
    out = sanitize_for_telegram("текст</b>без открытия")
    assert out  # не бросает исключение, что-то возвращает


def test_lab_comparison_operators_escaped():
    out = sanitize_for_telegram("глюкоза < 5.5, давление > 120")
    assert "&lt; 5.5" in out
    assert "&gt; 120" in out


def test_real_tag_not_confused_with_comparison():
    out = sanitize_for_telegram("<b>важно</b>")
    assert out == "<b>важно</b>"  # не превратилось в &lt;b&gt;


def test_excess_blank_lines_collapsed():
    out = sanitize_for_telegram("строка1\n\n\n\n\nстрока2")
    assert "\n\n\n" not in out


def test_empty_text_returns_as_is():
    assert sanitize_for_telegram("") == ""


def test_nested_tags_balanced_in_order():
    out = sanitize_for_telegram("<b><i>жирный курсив без закрытия")
    assert out == "<b><i>жирный курсив без закрытия</i></b>"
