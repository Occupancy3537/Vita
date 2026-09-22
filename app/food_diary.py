"""Порт n8n `🍽️ Food diary_v5` (2026-09-21, самый сложный воркфлоу проекта,
сознательно последний — 33 ноды). Telegram-бот приёма пищи: фото/текст/
фото+текст блюда -> LLM-анализ нутриентов -> запись в health.meals ->
подтверждение с кнопками (Подтвердить/Удалить/Изменить последний) ->
редактирование через reply на служебное сообщение с встроенным [ID:xxx].

ВАЖНО, ЕЩЁ НЕ РЕШЕНО (см. AGENT_SYNC.md #37): этот модуль содержит ТОЛЬКО
чистую логику (парсинг callback/сообщений, построение промптов, разбор
ответа LLM, статистику day/week, SQL для health.meals) — полностью
протестирован без единого живого вызова. НЕ подключён к реальному Telegram-
боту: `Food diary` использует СВОЙ credential (telegramApi 8CKBKo8CXTaLD3YI,
"vlad_health") — ДРУГОЙ бот, не тот, что уже слушает card-service (доктор,
Hermes Agent). Подключение живого бота (расшифровка токена, polling vs
webhook, скачивание фото) — отдельное решение, требует явного разговора с
Владом, как и было с Calendar-credential для группы 3: это ЖИВОЙ, каждый
день используемый ботом-собеседником, а не пассивный источник данных.

НАЙДЕНО при разборе, ЕЩЁ НЕ РЕШЕНО чинить ли: "Switch Message Types" в
оригинале — 5 НЕЗАВИСИМЫХ условий (reply/command/photo+caption/text-only/
photo-only), n8n Switch по умолчанию шлёт item на ВСЕ совпавшие выходы, не
первый совпавший. Ответ-с-фото на forceReply-подсказку теоретически попал
бы СРАЗУ на путь "правка" (prep) И на путь "текст и фото" (новый анализ) —
двойная обработка одного сообщения. Проверил execution_entity — ни разу не
встретилось в реальных логах (что не доказывает отсутствие, saveDataSuccess
=none прячет успешные прогоны). Порт classify_message() возвращает ПЕРВОЕ
совпадение по приоритету reply > command > photo+text > text > photo —
детерминированная, безопасная интерпретация (правка приоритетнее случайного
повторного анализа), но это ОСОЗНАННОЕ отличие от оригинала, не 1:1 перенос
— решение окончательно принимает Влад.

НАЙДЕНО, реальный дефект оригинала: сообщение-подтверждение ("answer" node,
после первичной записи блюда) НЕ содержит тег "[ID:...]" нигде в тексте —
только forceReply-подсказка от кнопки "Изменить последний" его содержит.
Значит прямой reply на САМО подтверждение (без нажатия кнопки "Изменить")
не находит запись для правки — extractEditId() возвращает None, и правка
пользователя рискует тихо потеряться (апдейт по Entry_ID=None ничего не
находит). Порт извлечения ID сохранён 1:1 (не чиню молча) — извлекает и
из подтверждения (сработает после исправления форматирования), но пока в
БД лежит текст в старом формате, поведение то же, что в оригинале.

2026-09-22 (по запросу Влада): слит с app/diet_tagger.py (порт n8n `Diet
Quality Tagger`, был отдельным воркфлоу/потоком раз в 15 мин). Раньше блюдо
записывалось в два прохода: этот модуль извлекал нутриенты сразу, а
diet_tagger отдельным ПОЗЖЕ (до 15 мин лагом) LLM-вызовом по уже
сохранённому Meal_description доклассифицировал NOVA/veg_g/fruit_g/
wholegrain_g/legume_nut_g/redmeat_g/ssb_ml/ПНЖ/plants и дописывал их же
UPDATE'ом — Влад справедливо назвал это "странной логикой" (два вызова LLM
на один приём пищи вместо одного). Теперь SYSTEM_PROMPT одним запросом
просит модель вернуть и нутриенты, и эту же классификацию; правила
классификации перенесены из diet_tagger._RULES дословно, нормализация
(клэмп NOVA 1..4, округление до 0.1, plants до 300 символов, переименование
LLM-ключа "pufa_g" в колонку "ПНЖ") — в normalize_food_group_tags(), тот же
алгоритм, что раньше жил в diet_tagger.parse_tags(). Отдельный модуль/
планировщик (DIET_TAGGER_ENABLED) удалён — на момент слияния очередь
необработанных строк (health.meals с пустым NOVA) была пуста, бэкфилла
старых записей не делаем (см. AGENT_SYNC — "новые метрики не бэкфилятся").
Побочный эффект: _sync_to_sheet() в food_diary_bot.py дублирует ВЕСЬ dict
`parsed` в Sheets, включая новые normalize_food_group_tags()-поля — это
закрывает ранее отмеченный пробел (diet_tagger писал NOVA/veg_g/... только
в Postgres, в Sheets эти колонки после переезда с n8n не попадали).

Заодно (по тому же запросу): раньше на фото и на голый текст были ДВЕ разные
модели (google/gemini-3.1-flash-lite на фото, z-ai/glm-5.3-flash на тексте) —
теперь везде одна MODEL = google/gemini-3.1-flash-lite, GLM 5.3 Flash в
питании больше не используется нигде."""
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.dashboard import _js_round

VL = timezone(timedelta(hours=10))

# 2026-09-22: раньше фото и текст ходили в разные модели (GLM 5.3 Flash на
# тексте, Gemini Flash Lite на фото) — по запросу Влада оставлена только
# одна, единая для питания.
MODEL = "google/gemini-3.1-flash-lite"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]

# Порт схемы JSON из системного промпта "AI Диетолог текст"/"AI Диетолог фото" —
# идентичный текст в обоих узлах оригинала, здесь одна константа.
NUTRIENT_FIELDS = [
    "Meal_description", "Calories", "Proteins", "Carbs", "Fats", "Магний", "Витамин D",
    "Омега-3 (EPA/DHA)", "Селен", "Йод", "Калий", "Железо", "Кальций", "Витамин B12",
    "Витамин К", "Витамин Е", "Цинк", "Клетчатка", "Холестерин", "Добавленный сахар",
    "Натрий", "Кофеин", "Алкоголь", "Насыщенные жиры", "Трансжиры",
]

# 2026-09-22: перенесено из app/diet_tagger.py (удалён при слиянии) — колонки
# health.meals для качества рациона, теперь заполняются В ТОМ ЖЕ LLM-вызове,
# что и NUTRIENT_FIELDS. "pufa_g" — ключ, которым модель называет это поле в
# JSON-ответе (то же, что раньше просил diet_tagger); в health.meals колонка
# называется "ПНЖ" — переименование делает normalize_food_group_tags().
FOOD_GROUP_LLM_KEYS = [
    "NOVA", "veg_g", "fruit_g", "wholegrain_g", "legume_nut_g", "redmeat_g", "ssb_ml", "pufa_g", "plants",
]

# Порт diet_tagger._RULES (2026-09-20) дословно — те же формулировки, тот же
# алгоритм классификации NOVA/групп продуктов, что раньше уходил отдельным
# LLM-вызовом раз в 15 мин по уже сохранённому Meal_description.
_FOOD_GROUP_RULES = [
    'NOVA — степень обработки: 1 необработанные/минимально обработанные (свежие, сушёные, мороженые овощи, фрукты, крупы, бобовые, орехи, яйца, молоко, мясо, рыба, натуральный йогурт без добавок); 2 кулинарные ингредиенты (растит. и слив. масло, сахар, мёд, соль, уксус) — используются как заправка/приправа к основе; 3 обработанные (хлеб, сыр, консервы, соленья, копчёности, домашняя или простая выпечка/кондитерка на муке-масле-сахаре-яйцах); 4 ультра-обработанные (снеки, газировка и сладкие напитки, колбаса/сосиски, готовые блюда, фастфуд, сухие завтраки, промышленная выпечка/кондитерка с маргарином/комбижиром/пальмовым маслом/эмульгаторами/консервантами, всё с длинным составом и промышленными добавками). ВАЖНО про выпечку, печенье, пастилу и подобное: НЕ относи к 4 автоматически по одному названию категории — решай по реальному составу, который есть в описании. Если сказано "домашний/домашняя/сам испёк" или перечислен простой состав (мука, масло/сливочное масло, сахар, яйца, без маргарина/пальмового масла/эмульгаторов/консервантов) — это 1-3, НЕ 4, даже если по форме это "печенье" или "пирог". Если продукт брендовый/покупной и точный состав в описании НЕ указан — НЕ придумывай конкретные ингредиенты (эмульгаторы, сиропы, консерванты и т.п.), которых нет в тексте; по умолчанию клади такой продукт в 3 (обработанные), поднимай до 4 только при явном признаке в самом описании (указан длинный/промышленный состав, консерванты, "магазинное"/"фабричное") или если это заведомо ультра-обработанная категория (газировка, чипсы, колбаса, фастфуд, сухие завтраки). Смешанное блюдо -> НАИВЫСШАЯ из групп, реально присутствующих в составе, а НЕ группа с наибольшей калорийностью: если в домашнем блюде из мяса/круп/овощей (NOVA 1) добавлены соль, масло или сахар — блюдо минимум NOVA 2, даже если основа даёт почти все калории. Аналогично: хлеб/сыр/консервы в составе -> минимум 3, любой ультра-обработанный компонент -> 4.',
    'veg_g — овощи БЕЗ картофеля (г). fruit_g — фрукты и ягоды (г), сок НЕ считать. wholegrain_g — цельнозерновые (цельнозерновой хлеб, овсянка, гречка, бурый рис, киноа, перловка); белый хлеб/рис/макароны/манка = 0. legume_nut_g — бобовые + орехи + семена (г). redmeat_g — красное (говядина, свинина, баранина) + переработанное мясо (колбаса, сосиски, бекон, ветчина) (г). ssb_ml — сладкие напитки + фруктовый сок (мл). pufa_g — полиненасыщенные жиры (омега-6 + растительная омега-3 ALA, БЕЗ EPA/DHA), г, оценка.',
    'Если в описании нет количества — оцени по типичной порции. Все числа без единиц измерения.',
    'plants — строка: перечисли ВСЕ разные виды растений в блюде через запятую. Название — ОДНО обобщённое слово в именительном падеже единственном числе, БЕЗ прилагательных: "перец" (не "болгарский перец"), "капуста" (не "белокочанная капуста"), "лук" (не "репчатый лук"), "томат" (не "помидоры черри"). Травы и специи считаются ("корица", "укроп"). Кофе/чай/какао НЕ включай. Пустая строка, если растений нет.',
]

SYSTEM_PROMPT = """Ты — профессиональный ИИ-нутрициолог и аналитик данных. Твоя задача — анализировать изображения или описания еды, определять состав блюда и выдавать данные в строгом формате JSON для импорта в Google Таблицы.

### ИНСТРУКЦИИ ПО АНАЛИЗУ:
1. **Распознавание:** Определи основные ингредиенты блюда.
2. **Оценка веса:** Визуально оцени вес порции (в граммах), основываясь на стандартной посуде (тарелка ~25см, столовая ложка и т.д.). Учитывай способ приготовления (жарка добавляет калории за счет масла, варка может уменьшать вес).
3. **Расчет нутриентов:** Используй усредненные данные из авторитетных баз (USDA, FoodData Central) для рассчитанных ингредиентов и веса.
4. **Микроэлементы:** Дай оценку содержания микроэлементов. Если продукт не является источником нутриента, ставь 0.
   - Единицы измерения: Калории (ккал), БЖУ и Клетчатка (граммы), Минералы, Холестерин, Натрий, Кофеин и Омега-3 (мг), Витамины (мкг), Алкоголь (г).
   - **Жиры:** Из состава общих жиров ("Fats") вытаскивай и записывай отдельно подкатегории: "Насыщенные жиры" (г) и "Трансжиры" (г). Сумма этих двух колонок не обязана равняться общим жирам, так как остаток — это ненасыщенные жиры.
   - **Добавленный сахар:** Считай только добавленный сахар (г) (сиропы, сахар, мед, добавки). Естественный сахар из фруктов/овощей/молочки сюда не включай.
   - **Омега-3 (EPA/DHA):** Строго разделяй морские и растительные источники. Для рыбы и морепродуктов указывай 100% от расчетного значения ЭПК/ДГК. Для растительных источников (грецкие орехи, льняное семя, чиа, растительные масла) рассчитай общее количество АЛК (ALA), умножь его на коэффициент конверсии 0.05 (5%) и запиши в JSON только эту итоговую цифру.

### ТРЕБОВАНИЯ К ВЫВОДУ (JSON):
1. Выведи ТОЛЬКО код JSON, обернутый в стандартный тег markdown: ```json [твой код] ```. Никаких приветствий, финальных фраз или пояснений вне этого блока быть не должно.
2. Ключи должны точно соответствовать названиям колонок в таблице (соблюдай регистр и язык, как в шаблоне ниже).
3. Все числовые значения должны быть типом `number` (без кавычек, без единиц измерения в тексте).
4. Если значение неизвестно или ничтожно мало, используй `0`.
5. В поле `Meal_description` кратко опиши блюдо и укажи предполагаемый вес (например: "Овсяная каша на молоке, 250г"). Если пользователь в своём тексте/подписи указал происхождение (домашнее / покупное, название бренда) или состав (например «без маргарина», «только мука, масло, сахар, яйца», «состав: яблоки, сахар») — ОБЯЗАТЕЛЬНО перенеси эту деталь в `Meal_description` своими словами, не теряй её при пересказе: по этой строке в ЭТОМ ЖЕ ответе определяется степень промышленной обработки блюда (см. ниже), и без этой детали её не определить верно.

### КЛАССИФИКАЦИЯ КАЧЕСТВА РАЦИОНА (в том же JSON, поля NOVA/veg_g/.../plants):
- """ + "\n- ".join(_FOOD_GROUP_RULES) + """

### СТРУКТУРА JSON:
```json
{
  "Meal_description": "Строка с описанием и весом",
  "Calories": 0, "Proteins": 0, "Carbs": 0, "Fats": 0, "Магний": 0, "Витамин D": 0,
  "Омега-3 (EPA/DHA)": 0, "Селен": 0, "Йод": 0, "Калий": 0, "Железо": 0, "Кальций": 0,
  "Витамин B12": 0, "Витамин К": 0, "Витамин Е": 0, "Цинк": 0, "Клетчатка": 0,
  "Холестерин": 0, "Добавленный сахар": 0, "Натрий": 0, "Кофеин": 0, "Алкоголь": 0,
  "Насыщенные жиры": 0, "Трансжиры": 0,
  "NOVA": 1, "veg_g": 0, "fruit_g": 0, "wholegrain_g": 0, "legume_nut_g": 0,
  "redmeat_g": 0, "ssb_ml": 0, "pufa_g": 0, "plants": ""
}
```"""

EDIT_ID_TAG_RX = re.compile(r"\[ID:(\d+)\]")
EDIT_ID_LOOSE_RX = re.compile(r"ID[^\d]*(\d+)", re.I)
OLD_DESC_RX = re.compile(r"🍽 (.*?)\n")


# =====================================================================
# 1. Классификация входящего апдейта (message vs callback_query, тип сообщения)
# =====================================================================

def is_callback(update: dict) -> bool:
    return bool(update.get("callback_query"))


def classify_message(message: dict) -> str:
    """Порт "Switch Message Types" — см. предупреждение в докстринге модуля
    про порядок приоритета (reply > command > photo+text > text > photo),
    ОСОЗНАННОЕ отличие от оригинальных независимых условий."""
    if message.get("reply_to_message"):
        return "reply"
    text = message.get("text")
    if text and text.startswith("/"):
        return "command"
    has_photo = message.get("photo") is not None
    caption_or_text = message.get("caption") is not None or text is not None
    if has_photo and caption_or_text:
        return "text_and_photo"
    if text is not None and not has_photo and not text.startswith("/"):
        return "text"
    if has_photo and message.get("caption") is None:
        return "photo"
    return "unknown"


def parse_callback_data(data: str) -> tuple[Optional[str], Optional[str]]:
    """Порт "Code" (callback-разбор): 'confirm|123' -> ('confirm', '123')."""
    parts = (data or "").split("|")
    action = parts[0] or None
    row_id = parts[1] if len(parts) > 1 else None
    return action, row_id


def build_answer_text(action: Optional[str]) -> str:
    return "Жду новые данные" if action == "edit" else "Принято"


# =====================================================================
# 2. Промпты для LLM (текст/фото/фото+текст/правка)
# =====================================================================

def build_text_prompt(user_text: str) -> str:
    return (f'Пользователь описал еду текстом: "{user_text}". Оцени вес, способ приготовления и состав. '
            "Верни ТОЛЬКО JSON строго по схеме и правилам из системного промпта (все поля; числа без единиц; неизвестное = 0; plants — строка).")


def build_photo_prompt(user_text: str) -> str:
    if user_text:
        return (f'Пользователь прислал фото еды с комментарием: "{user_text}". Используй комментарий для уточнения состава, веса и способа приготовления. '
                "Верни ТОЛЬКО JSON строго по схеме и правилам из системного промпта (все поля; числа без единиц; неизвестное = 0; plants — строка).")
    return ("Пользователь прислал фото еды. Оцени вес, способ приготовления и состав по изображению. "
            "Верни ТОЛЬКО JSON строго по схеме и правилам из системного промпта (все поля; числа без единиц; неизвестное = 0; plants — строка).")


def build_edit_prompt(original_text: str, correction: str) -> str:
    return (f'Пользователь вносит правку в блюдо. \nВот старое описание: "{original_text}". \n'
            f'Вот правка пользователя: "{correction}". \nПересчитай ВСЕ параметры: калории, БЖУ, полезные и вредные жиры, '
            "клетчатку, сахар, ЛПВП, ЛПНП), натрий, кофеин, алкоголь и все микроэлементы. Верни ТОЛЬКО строгий JSON со ВСЕМИ полями.")


def extract_edit_context(reply_text: str) -> tuple[Optional[str], str]:
    """Порт "prep" — вытаскивает Entry_ID из тега "[ID:nnn]" и старое описание
    блюда из строки "🍽 ..." в тексте сообщения, на которое ответил
    пользователь."""
    id_match = EDIT_ID_TAG_RX.search(reply_text or "")
    entry_id = id_match.group(1) if id_match else None
    desc_match = OLD_DESC_RX.search(reply_text or "")
    old_description = desc_match.group(1) if desc_match else "неизвестно"
    return entry_id, old_description


def extract_loose_entry_id(reply_text: Optional[str]) -> Optional[str]:
    """Порт регэкспа Entry_ID в "Только Обновление"/"Только Обновление PG":
    /ID[^\\d]*(\\d+)/i — тот же тег, но менее строгий (без квадратных
    скобок), применяется к тексту сообщения, на которое ответили."""
    if not reply_text:
        return None
    m = EDIT_ID_LOOSE_RX.search(reply_text)
    return m.group(1) if m else None


# =====================================================================
# 3. Разбор ответа LLM (Parse JSON from AI + AI Диетолог текст Parse)
# =====================================================================

def extract_llm_text(response_json: dict) -> str:
    """Порт "AI Диетолог текст Parse" (для httpRequest-варианта, текстовый
    путь) — OpenRouter chat/completions ответ."""
    choices = response_json.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def parse_json_from_ai(raw_text: str) -> dict:
    """Порт "Parse JSON from AI" — ищет JSON-блок в тексте (между первой { и
    последней }), парсит. Бросает ValueError, если не нашёл (оригинал бросал
    Error, останавливая workflow — тот же эффект: без валидного JSON от AI
    писать в meals нечего)."""
    if not raw_text:
        raise ValueError("Текст не найден")
    start_idx = raw_text.find("{")
    end_idx = raw_text.rfind("}")
    if start_idx == -1 or end_idx == -1:
        raise ValueError("JSON не найден")
    return json.loads(raw_text[start_idx:end_idx + 1])


def _food_group_num(v) -> float:
    """Порт diet_tagger._num — округление до 0.1 через _js_round (JS
    Math.round-семантика, не Python round-half-to-even)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n):
        return 0.0
    return _js_round(n * 10) / 10


def normalize_food_group_tags(parsed: dict) -> dict:
    """Порт diet_tagger.parse_tags() (нормализация), применённый к dict,
    который уже целиком распарсен parse_json_from_ai() — раньше это была
    ОТДЕЛЬНАЯ разборка ОТДЕЛЬНОГО ответа модели (отдельный поздний LLM-
    вызов), теперь модель отдаёт эти поля вместе с нутриентами в одном
    ответе, здесь только приведение к формату колонок health.meals: клэмп
    NOVA к 1..4 (со откатом на 1, если поле отсутствует/не число/0 — тот же
    JS `Math.round(Number(j.NOVA) || 1)`), округление количеств,
    "pufa_g" -> колонка "ПНЖ", plants обрезан до 300 символов.

    В отличие от diet_tagger.parse_tags() (которая при отсутствии NOVA
    возвращала {} и откладывала строку на следующий 15-минутный тик), здесь
    отката "попробовать позже" больше нет — это те же поля того же ответа,
    что уже дал нутриенты; при их отсутствии клэмп/дефолты применяются
    сразу, а не блокируют запись всего приёма пищи."""
    try:
        nova_raw = float(parsed.get("NOVA"))
        if not math.isfinite(nova_raw) or nova_raw == 0:
            nova_raw = 1.0
    except (TypeError, ValueError):
        nova_raw = 1.0
    nova = max(1, min(4, _js_round(nova_raw)))
    return {
        "NOVA": nova, "veg_g": _food_group_num(parsed.get("veg_g")),
        "fruit_g": _food_group_num(parsed.get("fruit_g")),
        "wholegrain_g": _food_group_num(parsed.get("wholegrain_g")),
        "legume_nut_g": _food_group_num(parsed.get("legume_nut_g")),
        "redmeat_g": _food_group_num(parsed.get("redmeat_g")),
        "ssb_ml": _food_group_num(parsed.get("ssb_ml")),
        "ПНЖ": _food_group_num(parsed.get("pufa_g")),
        "plants": str(parsed.get("plants") or "").strip()[:300],
    }


# =====================================================================
# 4. Статистика /today, /week (Агрегация /day_week)
# =====================================================================

NORMS = {"cal": 2400, "prot": 160, "carb": 270, "fat": 90}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def generate_bar(current: float, target: float) -> str:
    segments = 10
    progress = min(round((current / target) * segments), segments) if target else 0
    percentage = round((current / target) * 100) if target else 0
    return "🟩" * progress + "⬜" * (segments - progress) + f" {percentage}%"


def build_stats_message(meals: list[dict], user_id: str, command: str, now: Optional[datetime] = None) -> dict:
    """Порт "Агрегация /day_week". `meals` — health.meals строки (User_ID,
    Date как YYYY-MM-DDTHH:MI text, Calories/Proteins/Carbs/Fats)."""
    now = now or datetime.now(VL)
    is_week = "week" in command
    today_str = now.date().isoformat()
    week_ago_str = (now.date() - timedelta(days=6)).isoformat()

    stats = {"cal": 0.0, "prot": 0.0, "carb": 0.0, "fat": 0.0}
    count = 0
    for row in meals:
        if row.get("User_ID") != user_id or not row.get("Date"):
            continue
        row_date = str(row["Date"])[:10]
        is_match = (week_ago_str <= row_date <= today_str) if is_week else (row_date == today_str)
        if is_match:
            stats["cal"] += _num(row.get("Calories"))
            stats["prot"] += _num(row.get("Proteins"))
            stats["carb"] += _num(row.get("Carbs"))
            stats["fat"] += _num(row.get("Fats"))
            count += 1

    multiplier = 7 if is_week else 1
    period_text = f"за 7 дней ({week_ago_str} — {today_str})" if is_week else f"за сегодня ({today_str})"

    if count > 0:
        lines = [
            f"📊 *Статистика {period_text}:*", "",
            f"🔥 *Калории:* {round(stats['cal'])} / {NORMS['cal'] * multiplier}",
            generate_bar(stats["cal"], NORMS["cal"] * multiplier), "",
            f"🥩 *Белки:* {round(stats['prot'])}г / {NORMS['prot'] * multiplier}г",
            generate_bar(stats["prot"], NORMS["prot"] * multiplier), "",
            f"🍚 *Углеводы:* {round(stats['carb'])}г / {NORMS['carb'] * multiplier}г",
            generate_bar(stats["carb"], NORMS["carb"] * multiplier), "",
            f"🥑 *Жиры:* {round(stats['fat'])}г / {NORMS['fat'] * multiplier}г",
            generate_bar(stats["fat"], NORMS["fat"] * multiplier),
        ]
        text = "\n".join(lines)
    else:
        text = f"📊 {'За последние 7 дней' if is_week else 'За сегодня'} записей не найдено."

    return {
        "text": text,
        "reply_markup": {"inline_keyboard": [[
            {"text": "☀️ Сегодня", "callback_data": "/today"},
            {"text": "📅 Неделя", "callback_data": "/week"},
        ]]},
    }


def telegram_user_id(user: dict) -> str:
    """Порт `${first_name} ${last_name}`.trim() — та же строка, что уже
    используется как User_ID везде в health.meals."""
    return f"{user.get('first_name', '')} {user.get('last_name') or ''}".strip()


# =====================================================================
# 5. SQL для health.meals (Новая запись PG / Только Обновление PG / del PG)
# =====================================================================

_INSERT_COLS = [
    "Entry_ID", "User_ID", "Date", "Meal_description", "Calories", "Proteins", "Carbs", "Fats",
    "Магний", "Витамин D", "Омега-3 (EPA/DHA)", "Селен", "Йод", "Калий", "Железо", "Кальций",
    "Витамин B12", "Витамин К", "Витамин Е", "Цинк", "Клетчатка", "Холестерин",
    "Добавленный сахар", "Натрий", "Кофеин", "Алкоголь", "Трансжиры", "Насыщенные жиры",
    # 2026-09-22: перенесено из diet_tagger.py — теперь заполняется в том же
    # INSERT, что и нутриенты (см. normalize_food_group_tags()).
    "NOVA", "veg_g", "fruit_g", "wholegrain_g", "legume_nut_g", "redmeat_g", "ssb_ml", "ПНЖ", "plants",
]


def insert_meal(cur, entry_id: str, user_id: str, date_iso: str, nutrients: dict) -> None:
    """Порт "Новая запись PG" — INSERT ... ON CONFLICT (Entry_ID) DO UPDATE
    (тот же эффект, что Sheets "append", только по-настоящему идемпотентно:
    повторная отправка того же message_id как Entry_ID не плодит дубль)."""
    qi = lambda s: '"' + s.replace('"', '""') + '"'
    placeholders = ["%s", "%s", "%s::timestamptz"] + ["%s"] * (len(_INSERT_COLS) - 3)
    set_clause = ", ".join(f"{qi(c)}=EXCLUDED.{qi(c)}" for c in _INSERT_COLS if c != "Entry_ID")
    query = (
        f"INSERT INTO health.meals ({', '.join(qi(c) for c in _INSERT_COLS)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({qi('Entry_ID')}) DO UPDATE SET {set_clause}, _synced_at=now()"
    )
    values = [entry_id, user_id, date_iso] + [nutrients.get(c) for c in _INSERT_COLS[3:]]
    cur.execute(query, values)


def update_meal(cur, entry_id: str, user_id: str, nutrients: dict) -> None:
    """Порт "Только Обновление PG"."""
    qi = lambda s: '"' + s.replace('"', '""') + '"'
    update_cols = [c for c in _INSERT_COLS if c not in ("Entry_ID", "Date")]
    set_clause = ", ".join(f"{qi(c)}=%s" for c in update_cols)
    query = f"UPDATE health.meals SET {set_clause}, _synced_at=now() WHERE {qi('Entry_ID')} = %s"
    values = [(user_id if c == "User_ID" else nutrients.get(c)) for c in update_cols] + [entry_id]
    cur.execute(query, values)


def delete_meal(cur, entry_id: str) -> None:
    """Порт "del PG"."""
    cur.execute('DELETE FROM health.meals WHERE "Entry_ID" = %s', (entry_id,))
