"""
Диспетчер (план §3.2 шаг 2): решает "это доктору или нет" ДО того, как n8n
вообще увидит сообщение — по прямому решению Влада (2026-09-16): card-service
классифицирует само, Capitan видит только пересланные копии не-докторских
апдейтов. Классификатор Capitan (GLM, тот же промпт) НЕ убран — остаётся как
перестраховка: если этот диспетчер ошибочно отправит СИМПТОМ в "other",
Capitan своим классификатором поймает SYMPTOM и вызовет /doctor/turn обратно
(узел "Call New Doctor" уже настроен). Обратного пути нет для противоположной
ошибки (TEST ошибочно уходит доктору) — поэтому детерминированные случаи
ниже (фото/документ, реплай на анамнез) ВСЕГДА уходят мимо LLM-классификатора,
ошибиться там не на чем.

Волна 3 (B2, 2026-09-18): фото/документ -> "registrar" (app/registrar.py —
полный разбор лаб-документов в card-service, замена пересылки в выключенный
Capitan, где они терялись). По-прежнему детерминированно, без LLM на этом
шаге — LLM классифицирует СОДЕРЖИМИЕ уже внутри регистратора (шаг 1).
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


def classify_category(text: str, has_image: bool, timeout: float = 8.0,
                      context_hint: str = "") -> str:
    api_key = os.environ["OPENROUTER_API_KEY"]
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(text=text, has_image=has_image)
    if context_hint:
        prompt += ("\n\nКОНТЕКСТ: бот задал пациенту вопрос менее 12 часов назад (фрагмент): "
                   f"«{context_hint[:300]}»\n"
                   "Если текст похож на ответ на этот вопрос — это SYMPTOM.")
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


def is_reply_to_bot(update: dict) -> bool:
    """Реплай именно на сообщение БОТА (не человека). Инцидент 18.09 09:05 VL:
    короткий ответ «нет, красных флагов никогда не было» классификатор без
    контекста отправил в "other" и сообщение потерялось (Capitan-страховка мертва).
    Порт Capitan Sticky Route: реплай на сообщение бота = продолжение разговора
    с доктором, детерминированно, без LLM."""
    msg = update.get("message") or {}
    reply = msg.get("reply_to_message") or {}
    sender = reply.get("from") or {}
    return bool(reply) and bool(sender.get("is_bot"))


def recent_bot_question(chat_id) -> str:
    """Последний вопрос бота (<12 ч) — контекст для классификатора, чтобы ГОЛОСНЫЕ
    (не-реплай) короткие ответы на вопросы доктора не улетали в "other".
    Ошибки чтения глушим: подсказка — улучшение, не гарантия."""
    try:
        from app.db import get_conn, schema
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT left(text, 300) FROM {schema()}.dialog_turn "
                "WHERE chat_id = %s AND role = 'assistant' AND ts > now() - interval '12 hours' "
                "AND text LIKE '%%?%%' ORDER BY ts DESC LIMIT 1",
                (str(chat_id),),
            )
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def route(update: dict) -> str:
    """"doctor" -> intake.handle_update() в этом же процессе.
    "anamnesis" -> anamnesis.handle_reply() (детерминированно, без LLM; Волна 2/B1 —
    раньше такие реплаи пересылались в Capitan и терялись при его выключении).
    "registrar" -> registrar.handle_update() (детерминированно, без LLM здесь;
    Волна 3/B2 — раньше фото/документы пересылались в Capitan и терялись).
    "other" -> переслать сырой update на внутренний вебхук Capitan без изменений."""
    msg = update.get("message") or {}
    if msg.get("photo") or msg.get("document"):
        return "registrar"
    if is_anamnesis_reply(update):
        return "anamnesis"
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        # Голосовое, стикер и т.п. — не фото/документ (иначе уже отфильтровано
        # выше), не про занесение данных. Старый доктор на голосовое просил
        # продублировать текстом (план §1 п.8) — тот же случай, доктору есть
        # что ответить, регистратору просто нечего разбирать без текста/файла.
        return "doctor"
    # Sticky (порт Capitan Sticky Route, инцидент 18.09): реплай на сообщение
    # бота — это продолжение разговора с доктором. Слэш-команды не перехватываем.
    if is_reply_to_bot(update) and not text.startswith("/"):
        return "doctor"
    category = classify_category(
        text, has_image=False,
        context_hint=recent_bot_question((msg.get("chat") or {}).get("id")),
    )
    return "doctor" if category in DOCTOR_CATEGORIES else "other"
