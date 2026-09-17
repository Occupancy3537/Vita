"""
Диспетчер (план §3.2 шаг 2): решает "это доктору или нет" ДО того, как n8n
вообще увидит сообщение — по прямому решению Влада (2026-09-16): card-service
классифицирует само, Capitan видит только пересланные копии не-докторских
апдейтов. Классификатор Capitan (GLM, тот же промпт) НЕ убран — остаётся как
перестраховка: если этот диспетчер ошибочно отправит СИМПТОМ в "other",
Capitan своим классификатором поймает SYMPTOM и вызовет /doctor/turn обратно
(узел "Call New Doctor" уже настроен). Обратного пути нет для противоположной
ошибки (TEST ошибочно уходит доктору) — поэтому детерминированные случаи
ниже (фото/документ, реплай на анамнез) ВСЕГДА уходят в "other", без вызова
модели, ошибиться там не на чем.
"""
import os
import re

import httpx

from app.doctor import config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_ANAM_REPLY_RE = re.compile(r"#A\d\d")

# Промпт портирован дословно из узла "Текст" воркфлоу 🚨Capitan — не переписан,
# та же формулировка категорий, то же "при сомнении -> SYMPTOM".
CLASSIFY_PROMPT_TEMPLATE = """Ты — медицинский диспетчер-маршрутизатор. Классифицируй сообщение пользователя строго в одну категорию.

ВХОД:
- Текст: {text}
- Есть фото: {has_image}

ГЛАВНОЕ ПРАВИЛО ПРИ СОМНЕНИИ: если не уверен между TEST и SYMPTOM — выбирай SYMPTOM. Живой разговор о самочувствии важнее логирования.

TEST — только когда сообщение ЯВНО про занесение данных:
• Прямая команда: «запиши», «добавь в карту», «сохрани», «зафиксируй».
• Фото бланка анализов, заключения врача, этикетки состава продукта.
• Текст с КОНКРЕТНЫМИ результатами анализов: маркер + число + единица или референс (напр. «холестерин 5.5 ммоль/л», «ТТГ 2.1»).
Просто упоминание еды, БАДа или продукта в разговоре — это НЕ TEST.

SYMPTOM — всё про здоровье, жалобы и разговор с врачом (категория по умолчанию):
• Жалобы, симптомы, боль, усталость, дискомфорт.
• Вопрос-консультация по здоровью, «что это?», «что делать?», «можно ли добавку?».
• КОРОТКИЙ ОТВЕТ или УТОЧНЕНИЕ в продолжение разговора: описание где болит, что ел, когда началось, перечисление продуктов в ответ на вопрос врача — всё это SYMPTOM.
• Запрос на разбор медкарты, данных трекеров, сна, последних показателей.
• Фото симптома (сыпь, отёк) с вопросом «что это».

CALENDAR — расписание, записи к врачу, напоминания о приёме лекарств, запись приёма в календарь.

SLEEP — вопросы про качество/длительность сна, восстановление.

ОТВЕТ: одно слово из списка (TEST / SYMPTOM / CALENDAR / SLEEP). Без точек, кавычек, пояснений."""

DOCTOR_CATEGORIES = {"SYMPTOM", "CALENDAR", "SLEEP"}


def classify_category(text: str, has_image: bool, timeout: float = 8.0) -> str:
    api_key = os.environ["OPENROUTER_API_KEY"]
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(text=text, has_image=has_image)
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": config.DOCTOR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "provider": {"require_parameters": True, "order": config.DOCTOR_PROVIDER_ORDER,
                         "allow_fallbacks": True},
            "reasoning": {"effort": "low"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip().upper()
    for cat in ("TEST", "SYMPTOM", "CALENDAR", "SLEEP"):
        if cat in content:
            return cat
    return "SYMPTOM"  # то же правило "при сомнении", что и в самом промпте


def is_anamnesis_reply(update: dict) -> bool:
    """Та же проверка, что Anamnesis Gate в Capitan — реплай на сообщение с
    тегом #A01 и т.п. Детерминированно, без сети."""
    msg = update.get("message") or {}
    reply_text = ((msg.get("reply_to_message") or {}).get("text")) or ""
    return bool(_ANAM_REPLY_RE.search(reply_text))


def route(update: dict) -> str:
    """"doctor" -> intake.handle_update() в этом же процессе.
    "anamnesis" -> anamnesis.handle_reply() (детерминированно, без LLM; Волна 2/B1 —
    раньше такие реплаи пересылались в Capitan и терялись при его выключении).
    "other" -> переслать сырой update на внутренний вебхук Capitan без изменений."""
    msg = update.get("message") or {}
    if msg.get("photo") or msg.get("document"):
        return "other"
    if is_anamnesis_reply(update):
        return "anamnesis"
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        # Голосовое, стикер и т.п. — не фото/документ (иначе уже отфильтровано
        # выше), не про занесение данных. Старый доктор на голосовое просил
        # продублировать текстом (план §1 п.8) — тот же случай, доктору есть
        # что ответить, регистратору просто нечего разбирать без текста/файла.
        return "doctor"
    category = classify_category(text, has_image=False)
    return "doctor" if category in DOCTOR_CATEGORIES else "other"
