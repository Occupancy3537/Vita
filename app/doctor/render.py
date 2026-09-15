"""
Telegram-HTML санитайзер (план §3.9) — логика старого Sanitizer (Code-нода
`Sub-Agent: AI Doctor`) переосмыслена на Python, не скопирована построчно.
Решает реальные, неоднократно случавшиеся проблемы модели, а не гипотетические:
модель иногда пишет Markdown вместо HTML вопреки промпту, забывает закрыть тег
(Telegram отвечает 400 "Unmatched end tag" на весь запрос — без санитайзера это
роняет ответ целиком), использует `<`/`>` как знаки сравнения в лабах
("глюкоза < 5.5"), из-за чего Telegram пытается распарсить это как тег.

Применяется ко ВСЕМУ исходящему тексту (loop.py и emergency-ответ gate.py
одинаково) — для текста без тегов это no-op, отдельная ветка "не применять к
эмердженси" не нужна и не заводится.
"""
import re

_ALLOWED_TAGS = "b|strong|i|em|u|s|strike|del|a|code|pre"
_STRIP_DISALLOWED_RE = re.compile(rf"</?(?!(?:{_ALLOWED_TAGS})\b)[a-z0-9]+[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?([a-z]+)[^>]*>", re.IGNORECASE)
_BOLD_MD_RE = re.compile(r"\*\*(.*?)\*\*")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_P_RE = re.compile(r"</?p>", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def sanitize_for_telegram(text: str) -> str:
    if not text:
        return text

    text = _BR_RE.sub("\n", text)
    text = _P_RE.sub("\n\n", text)

    # Markdown-жирный, если модель соскользнула на него вопреки промпту.
    text = _BOLD_MD_RE.sub(r"<b>\1</b>", text)
    text = text.replace("*", "")

    # < / > как знаки сравнения (лабы, дозы) — до пробела/цифры это почти
    # наверняка математика, не начало тега.
    text = re.sub(r"<(?=\s|[0-9])", "&lt;", text)
    text = re.sub(r"(?<=\s|[0-9])>", "&gt;", text)

    # Только разрешённые Телеграмом теги — остальное (h1, div, span, ul...) прочь.
    text = _STRIP_DISALLOWED_RE.sub("", text)

    # Автобалансировка: незакрытый тег роняет ВЕСЬ ответ у Telegram (400,
    # "Unmatched end tag"), а не только форматирование — не косметика.
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(1).lower()
        is_closing = m.group(0).startswith("</")
        if not is_closing:
            stack.append(tag)
        elif tag in stack:
            # Последнее вхождение — как lastIndexOf в оригинале, не первое.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == tag:
                    del stack[i]
                    break
    for tag in reversed(stack):
        text += f"</{tag}>"

    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
