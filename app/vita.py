"""Vita v1 (2026-09-26) — «Сегодня» и «Ритм», перенос макета (Vita_PWA.html,
итерация 2, утверждён Владом) в прод как ОТДЕЛЬНОЙ страницы рядом со старым
дашбордом (app/dashboard.py). Старый дашборд НЕ трогается вообще — этот модуль
читает те же источники через уже существующие get_today_dashboard()/
get_health_dashboard()/get_today_nutrition() (app/dashboard.py), не копирует
и не дублирует их SQL. Только новое: композиция «скор дня» для кольца,
нудж-текст, разбор рычагов по блюдам дня, темп шагов — всё из уже посчитанных
или тривиально агрегируемых чисел (ПЛАН СБОРКИ в макете требует именно так:
«вся логика на сервере, фронтенд только рендерит»).

Табы «План», «Кейсы», «Я» в этом тикете не собираются (следующий тикет) —
здесь их нет ни на бэкенде, ни во фронтенде.
"""
import logging
import re
from typing import Optional

from psycopg import sql

from app import timeutil
from app.dashboard import (
    _STEPS_TARGET_DAILY,
    _num,
    get_health_dashboard,
    get_today_dashboard,
    get_today_nutrition,
)
from app.db import schema

logger = logging.getLogger(__name__)

# =====================================================================
# Скор дня (кольцо) — композиция уже существующих критериев, НЕ новая наука
# (ПЛАН СБОРКИ, п.3: «Кольцо Today = дневной вклад... нигде не смешивать»
# с PhenoAge; сам скор — отдельно). Формула и веса — прямой перенос scoreDay()
# со старого клиента дашборда (index.html до тикета «Пересборка вычитанием»,
# 2026-09-26): штраф ЭСКАЛИРУЕТ — один просевший критерий почти не трогает
# скор (−14), но каждый следующий стоит дороже предыдущего (до потолка −24) —
# "один сбой — не страшно, но если сыплется всё сразу, это должно быть видно
# сразу, не мягкой линейной шкалой". Годами проверено на живых данных Влада,
# не изобретено заново для Vita.
# =====================================================================

_BAD_STEP = [14, 18, 20, 22, 24, 24, 24, 24, 24]
_WARN_STEP = [4, 5, 6, 7, 8, 9, 9, 9, 9]
# Клинически значимые лимиты — только им разрешено тянуть скор как "bad", а не
# "warn" (тот же список CLIN, что был в index.html — не выдумывался заново).
_CLINICAL_LIMITS = {"Натрий", "Добавленный сахар", "Насыщенные жиры"}


def _step_sum(steps: list[int], k: int) -> int:
    return sum(steps[i] for i in range(min(k, len(steps))))


def _score_from_judgments(judgments: list[str]) -> Optional[int]:
    """None — критериев нет вообще (день не оценивается, не "оценка 0")."""
    if not judgments:
        return None
    bad = sum(1 for j in judgments if j == "bad")
    warn = sum(1 for j in judgments if j == "warn")
    return max(0, min(100, 100 - _step_sum(_BAD_STEP, bad) - _step_sum(_WARN_STEP, warn)))


def _budget_judgment(b: dict) -> str:
    if b.get("kind") != "limit":
        return "good"
    if b.get("status") == "over":
        return "bad" if b.get("label") in _CLINICAL_LIMITS else "warn"
    if b.get("status") in ("warn", "close"):
        return "warn"
    return "good"


def _collect_judgments(today: dict, health: dict) -> dict:
    """Критерии по ЧЕТЫРЁМ сегментам (recovery/sleep/move/food) + общий скор.
    Vita v2, этап 1 (2026-09-28): раньше Body Battery/ВСР (по сути —
    восстановление) не имели своего сегмента и просто падали в "overall" —
    v2 заводит отдельный кругляш "Восстановление" (CHIP_NORM.recovery в
    макете), поэтому им нужна отдельная категория. Группировка теперь по
    decision.reasons[].segment (app/dashboard.py проставляет его сам) —
    не строковый поиск "ACWR" in label, как было раньше (тот хак ломался
    бы на любую будущую правку подписи)."""
    decision = today.get("decision") or {}
    metric_by_key = {m["key"]: m for m in (health.get("metrics") or [])}
    budget = today.get("budget") or []
    limit_judgments = [_budget_judgment(b) for b in budget if b.get("kind") == "limit"]

    def j(key: str) -> Optional[str]:
        m = metric_by_key.get(key)
        return m.get("judgment") if m else None

    reasons = decision.get("reasons") or []
    reason_judgments = [r["judgment"] for r in reasons]
    recovery_judgments = [r["judgment"] for r in reasons if r.get("segment") == "recovery"]
    move_judgments = [r["judgment"] for r in reasons if r.get("segment") == "move"]

    overall = reason_judgments + [x for x in (j("sleep_min"), j("stress")) if x] + limit_judgments
    sleep = [x for x in (j("sleep_min"), j("sleep_score"), j("sleep_eff")) if x]
    recovery = recovery_judgments
    move = ([j("steps")] if j("steps") else []) + move_judgments
    food = limit_judgments

    return {"overall": overall, "sleep": sleep, "recovery": recovery, "move": move, "food": food}


def _scores(today: dict, health: dict) -> dict:
    crit = _collect_judgments(today, health)
    return {
        "score": _score_from_judgments(crit["overall"]),
        "sleep_score": _score_from_judgments(crit["sleep"]),
        "recovery_score": _score_from_judgments(crit["recovery"]),
        "movement_score": _score_from_judgments(crit["move"]),
        "nutrition_score": _score_from_judgments(crit["food"]),
    }


# =====================================================================
# ring.ahead — прогноз закрытия суток, ЕСЛИ главную подсказку выполнить
# (Vita v2, этап 1, Часть «Бэкенд-дельта» п.2). Честность обязательна —
# калибровочный тест на истории (app/vita_calibration.py) — гейт на выпуск,
# не формальность: систематическое расхождение >5 очков значит "не показывать
# ahead", а не "починим на лету косметикой".
# =====================================================================

def _main_action_segment(state: dict, steps: dict, protein: dict) -> Optional[str]:
    """Тот же приоритет, что build_nudge() ниже (не переизобретён отдельно —
    возвращает СЕГМЕНТ вместо готового текста). Пробелы в данных (нет часов/
    нет еды) не дают предсказания — "как будто появились данные" не то же
    самое, что "выполнил подсказку"."""
    if state["no_watch"] or state["no_food"] or state["time_of_day"] == "evening":
        return None
    left = None
    if protein.get("target") is not None and protein.get("consumed") is not None:
        left = protein["target"] - protein["consumed"]
    behind_pace = steps.get("behind_pace")
    if left and left > 20:
        return "food"
    if behind_pace:
        return "move"
    return None


def _score_with_segment_fixed(crit: dict, segment: str) -> Optional[int]:
    """Виртуально выполняет главную подсказку: ХУДШЕЕ суждение указанного
    сегмента заменяется на "good" (одно вхождение в overall — то же суждение,
    что реально входит в общий скор, не отдельный пересчёт весов)."""
    overall = list(crit["overall"])
    seg_judgments = crit.get(segment) or []
    worst = "bad" if "bad" in seg_judgments else ("warn" if "warn" in seg_judgments else None)
    if worst is None:
        return _score_from_judgments(overall)
    try:
        idx = overall.index(worst)
    except ValueError:
        return _score_from_judgments(overall)
    overall[idx] = "good"
    return _score_from_judgments(overall)


def compute_ahead(today: dict, health: dict, state: dict, steps: dict, protein: dict) -> Optional[int]:
    crit = _collect_judgments(today, health)
    current = _score_from_judgments(crit["overall"])
    if current is None:
        return None
    segment = _main_action_segment(state, steps, protein)
    if segment is None:
        return current
    return _score_with_segment_fixed(crit, segment)


# =====================================================================
# Состояние дня — ФЛАГИ, не тексты (Часть 2 тикета): фронтенд сам решает,
# какими словами макета показать каждую комбинацию.
# =====================================================================

def _time_of_day(now_local) -> str:
    """Пороги — по примерам макета (8:00 утро · 16:02 день · 21:30 вечер),
    не измеренная величина, календарное соглашение."""
    h = now_local.hour
    if h < 11:
        return "morning"
    if h < 19:
        return "day"
    return "evening"


def build_state(today: dict, now_local=None) -> dict:
    now_local = now_local or timeutil.now_local()
    decision = today.get("decision") or {}
    tod = _time_of_day(now_local)
    return {
        "time_of_day": tod,
        "no_watch": bool(decision.get("no_garmin_today")),
        "no_food": (today.get("meals_today") or 0) == 0,
        "sunday": now_local.weekday() == 6,
        "closed": tod == "evening",
        # «Нужен ты» / бейдж кейсов — в v1 всегда отсутствует (ticket Часть 2:
        # "могут быть всегда пустыми — блоки просто не рендерятся").
        "has_decision": False,
    }


# =====================================================================
# Гейт нагрузки — переиспользует то же decision.gate, что уже строит
# get_today_dashboard() (app.patient_gate.load_gate), вторая копия
# gate-логики не заводилась (ПЛАН СБОРКИ, п.2).
# =====================================================================

def build_gate(today: dict) -> dict:
    gate = (today.get("decision") or {}).get("gate") or {}
    if not gate.get("blocked"):
        return {"blocked": False}
    return {"blocked": True, "label": f"щадящий режим · {gate.get('condition', '').split(',')[0].split(' ')[-1] or 'режим'}"}


# =====================================================================
# Чипы: сон, ВСР к личной базе, энергия (Body Battery)
# =====================================================================

def build_chips(today: dict, health: dict, tn: dict) -> dict:
    metric_by_key = {m["key"]: m for m in (health.get("metrics") or [])}
    hrv = metric_by_key.get("hrv") or {}
    bb = metric_by_key.get("body_battery") or {}
    sleep_m = metric_by_key.get("sleep_min") or {}
    trend_word = None
    if hrv.get("judgment") == "good":
        trend_word = "растёт"
    elif hrv.get("judgment") == "bad":
        trend_word = "ниже базы"
    protein_consumed = _num((tn.get("summary") or {}).get("macros", {}).get("proteins", {}).get("consumed"))
    food_logged = (today.get("meals_today") or 0) > 0
    return {
        "sleep_min": sleep_m.get("value"),
        "hrv": {"value": hrv.get("value"), "trend": trend_word},
        "energy": bb.get("value"),
        "food_logged": food_logged,
        "protein_consumed": protein_consumed if food_logged else None,
    }


# =====================================================================
# Нудж — то, что уже считается (темп шагов, белок, жиры), формулировки
# из макета. Приоритет: часы важнее еды важнее темпа (нечего советовать
# по шагам, если день вообще не оценивается).
# =====================================================================

def build_nudge(state: dict, steps: dict, protein: dict, now_local) -> Optional[dict]:
    if state["no_watch"]:
        return {"tone": "neutral", "text": "Часы не видели тебя с утра — надеть?", "go": "watch"}
    if state["no_food"]:
        return {"tone": "neutral", "text": "Ждёт первого приёма — сфотографировать в дневнике.", "go": "protein"}
    if state["time_of_day"] == "evening":
        return None  # день закрыт — подсказывать поздно, коуч ниже уже сказал итог
    left = None
    if protein.get("target") is not None and protein.get("consumed") is not None:
        left = protein["target"] - protein["consumed"]
    behind_pace = steps.get("behind_pace")
    if behind_pace and left and left > 20:
        return {"tone": "apricot",
                "text": f"Прогулка после ужина поможет с темпом. Белок {round(protein['consumed'])}/{round(protein['target'])} — творог вечером.",
                "go": "protein"}
    if left and left > 20:
        return {"tone": "apricot", "text": f"Белок {round(protein['consumed'])}/{round(protein['target'])} — ещё один приём с творогом или курицей закроет цель.", "go": "protein"}
    if behind_pace:
        return {"tone": "apricot", "text": "Темп шагов чуть ниже обычного — короткая прогулка выправит день.", "go": None}
    return None


# =====================================================================
# Рычаги: жиры/белок/натрий — значения, цели, зоны, источники по блюдам,
# серии. Насыщенные жиры/Натрий — из today['budget'] (get_today_dashboard,
# не пересчитываются здесь); белок — из get_today_nutrition (тот же источник,
# что уже показывает старый дашборд на вкладке «Питание»).
# =====================================================================

_LEVER_META = {
    "fat": {"label": "Насыщенные жиры", "budget_label": "Насыщенные жиры", "col": "Насыщенные жиры", "streak_label": "Жиры в норме"},
    "sodium": {"label": "Натрий", "budget_label": "Натрий", "col": "Натрий", "streak_label": "Соль в норме"},
}


_MEAL_TYPE_RX = re.compile(r"^\s*(завтрак|обед|ужин|перекус|полдник|ланч)\b", re.I)


def _short_meal_label(desc: str, time_str: str) -> str:
    """Макет предполагает короткие подписи источника («Сыр», «Курица») —
    Meal_description в реальных данных это ПОЛНОЕ описание приёма пищи
    целиком (AI-регистратор пишет предложение, не список продуктов), короткого
    названия ингредиента в данных просто нет. Честная короткая подпись —
    название приёма пищи (оно реально есть первым словом в описании) + время,
    не выдуманный по названию продукт. Ряд в UI — 68px, длинный текст туда не
    влезает физически (см. .src .sb в CSS макета)."""
    m = _MEAL_TYPE_RX.match(desc or "")
    label = m.group(1).capitalize() if m else "Приём пищи"
    return f"{label} · {time_str}"


def _dish_sources(cur, col: str, limit: int = 4) -> list[tuple[str, float]]:
    """Топ приёмов пищи дня по вкладу в нутриент `col` — прямая агрегация
    health.meals за сегодня, ничего похожего не считалось раньше нигде
    (старый дашборд показывает только итог за день, не разбивку по приёмам)."""
    tz = timeutil.person_tz_name()
    cur.execute(
        sql.SQL('SELECT "Meal_description", {c}, to_char("Date" AT TIME ZONE %s, \'HH24:MI\') FROM health.meals '
                "WHERE (\"Date\" AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date "
                'AND {c} IS NOT NULL').format(c=sql.Identifier(col)),
        (tz, tz, tz),
    )
    rows = [(desc, _num(val), t) for desc, val, t in cur.fetchall() if _num(val)]
    rows.sort(key=lambda r: r[1], reverse=True)
    seen_labels: dict[str, int] = {}
    out = []
    for desc, val, t in rows[:limit]:
        label = _short_meal_label(desc, t)
        if label in seen_labels:
            seen_labels[label] += 1
            label = f"{label.split(' · ')[0]} {seen_labels[label]} · {t}"
        else:
            seen_labels[label] = 1
        out.append((label, val))
    return out


def _lever_note(key: str, status: str, top_source: Optional[str]) -> str:
    if key == "protein":
        if status == "done":
            return "Белок закрыт — держи темп."
        return f"Ещё один приём с белком закроет цель{f' — начни с {top_source.lower()}' if top_source else ''}."
    if status == "over":
        return f"{top_source} — основной источник сегодня, завтра можно полегче." if top_source else "Сегодня перебор — присмотрись к ужину."
    if status == "warn":
        return f"Есть запас, но {top_source.lower()} держит показатель наверху." if top_source else "Есть запас — не увлекайся."
    return "Сегодня менять ничего не нужно."


def build_levers(cur, today: dict, tn: dict) -> dict:
    budget_by_label = {b["label"]: b for b in (today.get("budget") or []) if b.get("kind") == "limit"}
    streaks_by_label = {s["label"]: s["count"] for s in (today.get("streaks") or [])}
    meals_today = today.get("meals_today") or 0

    out = {}
    for key, meta in _LEVER_META.items():
        b = budget_by_label.get(meta["budget_label"])
        if meals_today == 0 or b is None:
            out[key] = {"has_data": False}
            continue
        consumed, cap, unit = b["consumed"], b["cap"], b["unit"]
        status = "over" if b["status"] == "over" else ("warn" if b["status"] in ("warn", "close") else "ok")
        sources = _dish_sources(cur, meta["col"])
        top_name = sources[0][0] if sources else None
        out[key] = {
            "has_data": True, "label": meta["label"], "consumed": consumed, "target": cap, "unit": unit,
            "status": status, "streak_days": streaks_by_label.get(meta["streak_label"], 0),
            "sources": [{"name": n, "value": round(v, 1)} for n, v in sources],
            "note": _lever_note(key, status, top_name),
        }

    proteins = (tn.get("summary") or {}).get("macros", {}).get("proteins") or {}
    p_consumed, p_target = _num(proteins.get("consumed")), _num(proteins.get("target"))
    if meals_today == 0 or p_consumed is None or not p_target:
        out["protein"] = {"has_data": False}
    else:
        status = "done" if p_consumed >= p_target else "open"
        sources = _dish_sources(cur, "Proteins")
        out["protein"] = {
            "has_data": True, "label": "Белок", "consumed": p_consumed, "target": p_target, "unit": "г",
            "status": status, "streak_days": 0,
            "sources": [{"name": n, "value": round(v, 1)} for n, v in sources],
            "note": _lever_note("protein", status, sources[0][0] if sources else None),
        }
    return out


# =====================================================================
# Темп шагов — единственная РЕАЛЬНАЯ точка (live_steps_today), без
# фабрикации истории: почасового ряда шагов нигде не хранится (garminbot
# перезаписывает ОДНУ строку "шагов на сейчас" на дату, не копит историю
# внутри дня) — рисовать целый график "как было" значило бы придумывать
# данные. Целевая траектория — расчётная кривая (равномерный темп с лёгким
# ускорением к вечеру, тот же профиль, что в макете), не измерение.
# =====================================================================

VITA_STEPS_TARGET_GATED = 8000  # при активном мед. гейте — минус ~20% от базовой цели, тот же порядок, что в макете (12000 при 14540)


def build_steps(cur, gate: dict) -> dict:
    tz = timeutil.person_tz_name()
    cur.execute(
        "SELECT steps FROM health.live_steps_today WHERE date = (now() AT TIME ZONE %s)::date",
        (tz,),
    )
    row = cur.fetchone()
    steps_now = int(row[0]) if row and row[0] is not None else None
    target = VITA_STEPS_TARGET_GATED if gate.get("blocked") else _STEPS_TARGET_DAILY

    now_local = timeutil.now_local()
    now_hour = now_local.hour + now_local.minute / 60
    behind_pace = False
    if steps_now is not None and 6 <= now_hour <= 22:
        expected = target * ((now_hour - 6) / 16) ** 0.7
        behind_pace = steps_now < expected * 0.85
    return {
        "target": target, "now_hour": round(now_hour, 2), "now_steps": steps_now,
        "behind_pace": behind_pace,
        "status_word": (None if steps_now is None else
                        ("цель взята" if steps_now >= target else
                         ("чуть ниже темпа" if behind_pace else "по темпу"))),
    }


# =====================================================================
# Сборка ответов /vita/today и /vita/rhythm
# =====================================================================

def build_today(cur) -> dict:
    today = get_today_dashboard(cur)
    health = get_health_dashboard(cur)
    tn = get_today_nutrition(cur)
    state = build_state(today)
    gate = build_gate(today)
    steps = build_steps(cur, gate)
    levers = build_levers(cur, today, tn)
    protein = levers.get("protein") or {}
    nudge = build_nudge(state, steps, protein, timeutil.now_local())
    scores = _scores(today, health)
    ahead = compute_ahead(today, health, state, steps, protein)
    longevity = today.get("longevity") or {}
    bioage_days = round((longevity.get("affects_today_total") or 0) * 365, 1) if longevity else None

    return {
        "date": today.get("date"),
        "state": state,
        "gate": gate,
        "ring": {**scores, "ahead": ahead, "bioage_days": bioage_days},
        "chips": build_chips(today, health, tn),
        "nudge": nudge,
    }


def build_rhythm(cur) -> dict:
    today = get_today_dashboard(cur)
    tn = get_today_nutrition(cur)
    state = build_state(today)
    gate = build_gate(today)
    steps = build_steps(cur, gate)
    levers = build_levers(cur, today, tn)
    return {
        "date": today.get("date"),
        "state": state,
        "gate": gate,
        "steps": steps,
        "levers": levers,
    }


# =====================================================================
# Роуты — вход по паролю (httpOnly cookie, НЕ токен в URL) + сама страница
# и её два JSON-эндпоинта, всё за одной cookie-проверкой (Часть 1 тикета).
# =====================================================================

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.db import get_conn
from app.vita_auth import COOKIE_NAME, check_password, create_session_token, require_session, verify_session_token

router = APIRouter()
_STATIC_DIR = Path(__file__).parent / "static"
_html_cache: dict[str, str] = {}


def _read_static(name: str) -> str:
    if name not in _html_cache:
        _html_cache[name] = (_STATIC_DIR / name).read_text(encoding="utf-8")
    return _html_cache[name]


@router.get("/vita")
def vita_page(request: Request):
    """Пункт 2 Части 1 тикета: без cookie — 401 И на саму страницу, не только
    на API (страница — не публичный HTML, который просто не грузит данные).
    Валидная cookie есть -> отдаём приложение; нет -> редирект на форму входа,
    а не голый 401, чтобы человек с телефона сразу увидел, что вводить пароль."""
    if not verify_session_token(request.cookies.get(COOKIE_NAME, "")):
        return RedirectResponse(url="/vita/login")
    return HTMLResponse(_read_static("vita.html"))


@router.get("/vita/login", response_class=HTMLResponse)
def vita_login_page() -> str:
    return _read_static("vita_login.html")


class VitaLoginRequest(BaseModel):
    password: str = ""


@router.post("/vita/login")
def vita_login(req: VitaLoginRequest, response: Response) -> dict:
    if not check_password(req.password):
        raise HTTPException(status_code=401, detail="неверный пароль")
    token = create_session_token()
    response.set_cookie(
        COOKIE_NAME, token, max_age=90 * 24 * 3600, httponly=True, secure=True,
        samesite="lax", path="/vita",
    )
    return {"ok": True}


@router.post("/vita/logout")
def vita_logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/vita")
    return {"ok": True}


@router.get("/vita/today")
def vita_today_endpoint(_: None = Depends(require_session)) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            return build_today(cur)


@router.get("/vita/rhythm")
def vita_rhythm_endpoint(_: None = Depends(require_session)) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            return build_rhythm(cur)
