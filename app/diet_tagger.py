"""Порт n8n `Diet Quality Tagger` (2026-09-20, группа 2 — первый с LLM-
вызовом). Раз в 15 мин (интервал ноды, не её название "Every 10 min" — n8n
разошёлся с реальным параметром, оставляю настоящее значение) находит до 8
последних приёмов пищи без NOVA-разметки, просит модель классифицировать
(NOVA + овощи/фрукты/цельнозерновые/бобовые+орехи/красное мясо/сладкие
напитки/ПНЖ/список растений — то, что уже читает get_weekly_nutrition()'s
diet_quality/AHEI/plant-diversity), пишет обратно в health.meals.

2026-09-20: оригинал дублировал запись в Sheets Meals (`Write Tags`) вдобавок
к Postgres (`Write Tags PG`) — Sheets-сторону не переносим, ничего её больше
не читает (тот же принцип, что и везде в этой волне: Postgres канон, лист —
легаси-зеркало, которое не обновляем дальше)."""
import json
import logging
import math
import os
import time

import httpx

from app.dashboard import _js_round
from app.db import get_conn

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "z-ai/glm-5.3-flash"
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]
BATCH_SIZE = 8
INTERVAL_SECONDS = 15 * 60

_RULES = [
    'NOVA — степень обработки: 1 необработанные/минимально обработанные (свежие, сушёные, мороженые овощи, фрукты, крупы, бобовые, орехи, яйца, молоко, мясо, рыба, натуральный йогурт без добавок); 2 кулинарные ингредиенты (растит. и слив. масло, сахар, мёд, соль, уксус) — используются как заправка/приправа к основе; 3 обработанные (хлеб, сыр, консервы, соленья, копчёности, домашняя или простая выпечка/кондитерка на муке-масле-сахаре-яйцах); 4 ультра-обработанные (снеки, газировка и сладкие напитки, колбаса/сосиски, готовые блюда, фастфуд, сухие завтраки, промышленная выпечка/кондитерка с маргарином/комбижиром/пальмовым маслом/эмульгаторами/консервантами, всё с длинным составом и промышленными добавками). ВАЖНО про выпечку, печенье, пастилу и подобное: НЕ относи к 4 автоматически по одному названию категории — решай по реальному составу, который есть в описании. Если сказано "домашний/домашняя/сам испёк" или перечислен простой состав (мука, масло/сливочное масло, сахар, яйца, без маргарина/пальмового масла/эмульгаторов/консервантов) — это 1-3, НЕ 4, даже если по форме это "печенье" или "пирог". Если продукт брендовый/покупной и точный состав в описании НЕ указан — НЕ придумывай конкретные ингредиенты (эмульгаторы, сиропы, консерванты и т.п.), которых нет в тексте; по умолчанию клади такой продукт в 3 (обработанные), поднимай до 4 только при явном признаке в самом описании (указан длинный/промышленный состав, консерванты, "магазинное"/"фабричное") или если это заведомо ультра-обработанная категория (газировка, чипсы, колбаса, фастфуд, сухие завтраки). Смешанное блюдо -> НАИВЫСШАЯ из групп, реально присутствующих в составе, а НЕ группа с наибольшей калорийностью: если в домашнем блюде из мяса/круп/овощей (NOVA 1) добавлены соль, масло или сахар — блюдо минимум NOVA 2, даже если основа даёт почти все калории. Аналогично: хлеб/сыр/консервы в составе -> минимум 3, любой ультра-обработанный компонент -> 4.',
    'veg_g — овощи БЕЗ картофеля (г). fruit_g — фрукты и ягоды (г), сок НЕ считать. wholegrain_g — цельнозерновые (цельнозерновой хлеб, овсянка, гречка, бурый рис, киноа, перловка); белый хлеб/рис/макароны/манка = 0. legume_nut_g — бобовые + орехи + семена (г). redmeat_g — красное (говядина, свинина, баранина) + переработанное мясо (колбаса, сосиски, бекон, ветчина) (г). ssb_ml — сладкие напитки + фруктовый сок (мл). pufa_g — полиненасыщенные жиры (омега-6 + растительная омега-3 ALA, БЕЗ EPA/DHA), г, оценка.',
    'Если в описании нет количества — оцени по типичной порции. Все числа без единиц измерения.',
    'plants — строка: перечисли ВСЕ разные виды растений в блюде через запятую. Название — ОДНО обобщённое слово в именительном падеже единственном числе, БЕЗ прилагательных: "перец" (не "болгарский перец"), "капуста" (не "белокочанная капуста"), "лук" (не "репчатый лук"), "томат" (не "помидоры черри"). Травы и специи считаются ("корица", "укроп"). Кофе/чай/какао НЕ включай. Пустая строка, если растений нет.',
]


def _prompt_for(description: str) -> str:
    return (
        f'Классифицируй приём пищи для индексов качества рациона. Описание блюда:\n"{description}"\n\n'
        'Верни ТОЛЬКО JSON, без markdown и текста вокруг:\n'
        '{"NOVA":1,"veg_g":0,"fruit_g":0,"wholegrain_g":0,"legume_nut_g":0,"redmeat_g":0,"ssb_ml":0,"pufa_g":0,"plants":""}\n\n'
        'Правила:\n- ' + '\n- '.join(_RULES)
    )


def _num(v) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n):
        return 0.0
    return _js_round(n * 10) / 10


def pick_untagged(cur, limit: int = BATCH_SIZE) -> list[tuple]:
    cur.execute(
        'SELECT "Entry_ID", "Meal_description" FROM health.meals '
        "WHERE \"Meal_description\" IS NOT NULL AND btrim(\"Meal_description\") != '' "
        "AND (\"NOVA\" IS NULL OR btrim(\"NOVA\") = '')"
    )
    rows = [(str(eid), str(desc).strip()) for eid, desc in cur.fetchall() if eid]
    rows.sort(key=lambda r: float(r[0]) if r[0].replace(".", "", 1).isdigit() else -1, reverse=True)
    return rows[:limit]


def call_model(description: str, timeout: float = 20.0) -> dict:
    """Один запрос на один приём пищи (как в оригинале — Build Prompts/OpenRouter
    работали построчно, не батчем). Возвращает {} при любом сбое разбора —
    вызывающий просто не тегирует эту строку в этом тике, попробует в следующий."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {}
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL, "temperature": 0.1, "max_tokens": 900,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "reasoning": {"max_tokens": 400},
                "messages": [{"role": "user", "content": _prompt_for(description)}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = str(resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("diet_tagger: вызов модели упал")
        return {}
    return parse_tags(raw)


def parse_tags(raw: str) -> dict:
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        j = json.loads(raw[s:e + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(j, dict) or j.get("NOVA") is None:
        return {}
    # JS: Math.round(Number(j.NOVA) || 1) — Number() на мусоре даёт NaN, NaN||1
    # откатывается на 1, не бросает исключение; повторяю именно это, не "чиню".
    try:
        nova_raw = float(j["NOVA"])
        if not math.isfinite(nova_raw) or nova_raw == 0:
            nova_raw = 1.0
    except (TypeError, ValueError):
        nova_raw = 1.0
    nova = max(1, min(4, _js_round(nova_raw)))
    return {
        "NOVA": nova, "veg_g": _num(j.get("veg_g")), "fruit_g": _num(j.get("fruit_g")),
        "wholegrain_g": _num(j.get("wholegrain_g")), "legume_nut_g": _num(j.get("legume_nut_g")),
        "redmeat_g": _num(j.get("redmeat_g")), "ssb_ml": _num(j.get("ssb_ml")),
        "ПНЖ": _num(j.get("pufa_g")), "plants": str(j.get("plants") or "").strip()[:300],
    }


def write_tags(cur, entry_id: str, tags: dict) -> None:
    cur.execute(
        'UPDATE health.meals SET "NOVA"=%s, "veg_g"=%s, "fruit_g"=%s, "wholegrain_g"=%s, '
        '"legume_nut_g"=%s, "redmeat_g"=%s, "ssb_ml"=%s, "ПНЖ"=%s, "plants"=%s, _synced_at=now() '
        'WHERE "Entry_ID" = %s',
        (str(tags["NOVA"]), str(tags["veg_g"]), str(tags["fruit_g"]), str(tags["wholegrain_g"]),
         str(tags["legume_nut_g"]), str(tags["redmeat_g"]), str(tags["ssb_ml"]), str(tags["ПНЖ"]),
         tags["plants"], entry_id),
    )


def run_once() -> int:
    """Возвращает число реально протегированных строк (для тестов/логов)."""
    with get_conn() as conn, conn.cursor() as cur:
        rows = pick_untagged(cur)
    tagged = 0
    for entry_id, description in rows:
        tags = call_model(description)
        if not tags:
            continue
        with get_conn() as conn, conn.cursor() as cur:
            write_tags(cur, entry_id, tags)
            conn.commit()
        tagged += 1
    return tagged


def run_scheduler() -> None:
    logger.info("diet_tagger scheduler: старт")
    while True:
        try:
            n = run_once()
            if n:
                logger.info("diet_tagger: протегировано %s приёмов пищи", n)
        except Exception:
            logger.exception("diet_tagger run_once упал — повтор через %ss", INTERVAL_SECONDS)
        time.sleep(INTERVAL_SECONDS)
