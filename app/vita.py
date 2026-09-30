"""Vita v1 (2026-09-26) — «Сегодня» и «Ритм», перенос макета (Vita_PWA.html,
итерация 2, утверждён Владом) в прод как ОТДЕЛЬНОЙ страницы рядом со старым
дашбордом (app/dashboard.py). Старый дашборд НЕ трогается вообще — этот модуль
читает те же источники через уже существующие get_today_dashboard()/
get_health_dashboard()/get_today_nutrition() (app/dashboard.py), не копирует
и не дублирует их SQL. Только новое: композиция «скор дня» для кольца,
нудж-текст, разбор рычагов по блюдам дня, темп шагов — всё из уже посчитанных
или тривиально агрегируемых чисел (ПЛАН СБОРКИ в макете требует именно так:
«вся логика на сервере, фронтенд только рендерит»).

Vita v2 (сверка с макетом v7, 2026-09-28): «Врач» и «Я» собраны поверх уже
существующих источников — /vita/doctor, /vita/medpassport, /vita/me (см. ниже).
"""
import json
import logging
import re
import statistics
from datetime import timedelta
from typing import Optional

from psycopg import sql

from app import checks, timeutil
from app.dashboard import (
    _SLEEP_MAX_OK,
    _SLEEP_MIN_OK,
    _STEPS_TARGET_DAILY,
    _baseline,
    _baseline_metrics_for_index,
    _budget_for_day,
    _build_reasons,
    _load_mean,
    _num,
    _rows_as_dicts,
    get_health_dashboard,
    get_today_dashboard,
    get_today_nutrition,
)
from app.db import get_conn, schema

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
        """Живой баг, найденный при разборе «индекс дня зависит от
        кругляшей» (2026-09-28): _baseline_metrics_for_index кладёт запись
        с judgment='neutral' по умолчанию ДАЖЕ когда value=None (метрики
        реально нет за день) — раньше это было незаметно (neutral не даёт
        штрафа в escalation-формуле в любом случае, "нет данных" и "нейтрально"
        выглядели одинаково), но с новым _day_index (среднее по сегментам)
        фантомный "нейтральный" судимый критерий превращался в фантомную
        "сотню" для сегмента без единого реального числа. Судимость
        учитываем только если у метрики есть значение."""
        m = metric_by_key.get(key)
        if not m or m.get("value") is None:
            return None
        return m.get("judgment")

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


def _day_index(scores: dict, chips: dict) -> Optional[int]:
    """Живая правка Влада (2026-09-28): «кругляши поменяли, итоговый индекс
    дня нет, он от них зависит» — «Заряд»/«Сон» теперь показывают настоящие
    числа Гармина (chips.energy/sleep_quality), не судейский скор (см.
    build_chips/chip_status) — индекс дня обязан считаться из ТОГО ЖЕ, что
    видно в кругляшах, а не из параллельной системы штрафов. Среднее по 4
    сегментам: Заряд/Сон — реальное число (откат на судейский скор сегмента,
    если часы сегодня ничего не дали), Движение/Питание — по-прежнему
    судейский скор (для них нет готового композитного числа от часов).
    None только если ВСЕ 4 сегмента без данных — не "оценка 0"."""
    vals = [
        chips.get("energy") if chips.get("energy") is not None else scores.get("recovery_score"),
        chips.get("sleep_quality") if chips.get("sleep_quality") is not None else scores.get("sleep_score"),
        scores.get("movement_score"),
        scores.get("nutrition_score"),
    ]
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals)) if vals else None


def _segment_score_with_fixed(crit: dict, segment: str) -> Optional[int]:
    """Версия _score_with_segment_fixed для новой формулы индекса (среднее
    сегментов, не escalation по объединённому overall-списку) — считает
    СКОР ЭТОГО ЖЕ сегмента с виртуально исправленным худшим суждением, не
    общий overall. Нужно только для move/food — _main_action_segment
    никогда не целится в recovery/sleep (Body Battery/сон не чинятся
    сегодняшней подсказкой, см. её докстринг)."""
    seg_judgments = list(crit.get(segment) or [])
    if "bad" in seg_judgments:
        seg_judgments[seg_judgments.index("bad")] = "good"
    elif "warn" in seg_judgments:
        seg_judgments[seg_judgments.index("warn")] = "good"
    return _score_from_judgments(seg_judgments)


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


def compute_ahead(today: dict, health: dict, state: dict, steps: dict, protein: dict, chips: dict) -> Optional[int]:
    """Пересчитано под новую формулу индекса (_day_index — среднее сегментов
    с реальными числами Гармина для Заряда/Сна, см. её докстринг). chips
    здесь не меняются виртуально (Body Battery/сон не чинятся сегодняшней
    подсказкой) — фиксируется только скор actionable-сегмента (move/food)."""
    crit = _collect_judgments(today, health)
    scores = {
        "recovery_score": _score_from_judgments(crit["recovery"]),
        "sleep_score": _score_from_judgments(crit["sleep"]),
        "movement_score": _score_from_judgments(crit["move"]),
        "nutrition_score": _score_from_judgments(crit["food"]),
    }
    current = _day_index(scores, chips)
    if current is None:
        return None
    segment = _main_action_segment(state, steps, protein)
    if segment is None:
        return current
    score_key = _SCORE_KEY_BY_SEGMENT[segment]
    improved = {**scores, score_key: _segment_score_with_fixed(crit, segment)}
    return _day_index(improved, chips)


# =====================================================================
# «Из чего индекс» — шторка «Прогноз дня» в макете показывает КАЖДЫЙ критерий
# ✓/!/✗ с реальным значением (Vita v2, этап 1, ПЛАН СБОРКИ п.10/п.17). Флаги
# состояния (no_watch/closed и т.п.) фронтенд уже решает сам словами макета
# (см. докстринг build_state ниже — так было решено ещё в v1) — а вот
# конкретные подписи/значения критериев ("Сон 7:04", "Натрий 3,2 из 5 г")
# нигде в ответе не было, их не сочинить на фронте из одних скор-чисел.
# =====================================================================

_JUDGMENT_MARK = {"good": "y", "neutral": "y", "warn": "w", "bad": "n"}


def _plain_num(v) -> str:
    """80.0 -> «80», 6.2 -> «6.2» (в строках разбора не нужны лишние «.0»)."""
    if isinstance(v, float):
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return str(v)


def build_index_breakdown(today: dict, health: dict) -> list[dict]:
    """Vita v2, этап 2 (2026-09-28, живая жалоба Влада: «нажатие на питание,
    заряд, сон, движение открывает один экран») — каждая строка теперь несёт
    "segment", чтобы шторка конкретного кругляша (app/static/vita.html::
    B.segment) могла показать СВОИ критерии, а не весь список целиком (тот
    остаётся в B.index() — «Прогноз дня», ему положено быть полным)."""
    decision = today.get("decision") or {}
    metric_by_key = {m["key"]: m for m in (health.get("metrics") or [])}
    budget = today.get("budget") or []
    rows = []
    for r in (decision.get("reasons") or []):
        rows.append({"mark": _JUDGMENT_MARK.get(r["judgment"], "y"), "label": r["label"],
                     "detail": str(r.get("value", "")), "segment": r.get("segment")})
    for key, label, segment in (("sleep_min", "Сон", "sleep"), ("stress", "Стресс", "recovery")):
        m = metric_by_key.get(key)
        if m and m.get("value") is not None:
            rows.append({"mark": _JUDGMENT_MARK.get(m["judgment"], "y"), "label": label,
                         "detail": f"{_plain_num(m['value'])} {m.get('unit') or ''}".strip(), "segment": segment})
    for b in budget:
        if b.get("kind") != "limit":
            continue
        rows.append({"mark": _JUDGMENT_MARK[_budget_judgment(b)], "label": b["label"],
                     "detail": f"{_plain_num(b['consumed'])}/{_plain_num(b['cap'])} {b['unit']}", "segment": "food"})
    return rows


# =====================================================================
# Детали кругляша (Vita v2, этап 2, 2026-09-28) — живая просьба Влада после
# первого фикса ("список тот же, только отфильтрованный — в макете не так"):
# взять реальные шторки макета (recovery()/topic('sleep')/move()/nutr()) и
# подключить настоящие данные, а не просто резать общий breakdown. У каждого
# сегмента — своя история/статистика из health.daily_trends (была не нужна
# ДО этого момента — калибровке/сериям хватало последнего дня, здесь нужны
# графики) + реальные публикации card.publication по теме (157 строк в базе,
# не выдуманные). Честно НЕ реализовано (нет источника данных вообще):
# почасовой график шагов внутри дня (health.* хранит только суточный итог,
# см. health.live_steps_today) и недельная тепловая карта микронутриентов
# (нет референсных норм по каждому нутриенту в системе — не то же самое,
# что просто просуммировать историю). "Движение" ниже честно показывает
# ДНЕВНОЙ (не часовой) тренд шагов за неделю вместо этого.
# =====================================================================

_TOPIC_PUB_TERM = {"recovery": "heart rate variability", "sleep": "sleep",
                    "move": "physical activity", "food": "protein"}


def _recent_daily_values(cur, columns: list[str], days: int) -> list[dict]:
    """Последние `days` дней выбранных колонок health.daily_trends, по
    возрастанию даты — независимо от _fetch_history/_DAILY_TRENDS_COLS (та
    тяжелее, тянет ещё meals/targets, не нужные для графиков сегмент-шторок)."""
    cols = ", ".join(f'"{c}"' for c in (["Дата"] + columns))
    cur.execute(f'SELECT {cols} FROM health.daily_trends ORDER BY "Дата" DESC LIMIT %s', (days,))
    rows = [dict(zip(["Дата"] + columns, r)) for r in cur.fetchall() if r[0]]
    rows.reverse()
    return [{"date": r["Дата"].isoformat(), **{c: _num(r[c]) for c in columns}} for r in rows]


def _topic_publications(cur, segment: str, limit: int = 5) -> list[dict]:
    from app.consilium import _relevant_publications
    from app.research_scan import _GRADE_LABEL

    pubs = _relevant_publications(cur, _TOPIC_PUB_TERM.get(segment, ""), limit=limit)
    return [{"title": p["title"], "grade": _GRADE_LABEL.get(p.get("design_type"), p.get("design_type") or "тип не определён"),
             "why": p.get("why_for_you") or "", "url": p.get("url")} for p in pubs]


def build_recovery_detail(cur, today: dict, gate: dict) -> dict:
    history = _recent_daily_values(cur, ["ВСР_ночная"], 30)
    vals = [r["ВСР_ночная"] for r in history if r["ВСР_ночная"] is not None]
    rhr_row = _recent_daily_values(cur, ["Пульс_ночной_средний"], 1)
    # Живой баг, пойманный на самом себе (2026-09-28): acwr/acwr_status/
    # load_high лежат ВНУТРИ today["decision"] (dashboard.py::get_today_dashboard,
    # там же, где gate/reasons — build_gate() уже читал decision правильно,
    # эта функция — нет), не на верхнем уровне today. Читал плоско, всегда
    # получал None, хотя ACWR реально есть (0 · LOW сегодня).
    decision = today.get("decision") or {}
    acwr, acwr_status, load_high = decision.get("acwr"), decision.get("acwr_status"), decision.get("load_high")
    if load_high:
        coach = "Нагрузка выше обычного — восстановление сейчас в приоритете."
    elif acwr_status == "LOW":
        coach = "Нагрузка низкая, восстановление в порядке — можно держать текущий темп."
    else:
        coach = "Восстановление в норме, ограничений по нагрузке нет."
    if gate.get("blocked"):
        coach += f" {gate['label']} — без ударных нагрузок."
    return {
        "segment": "recovery",
        "hrv_last": vals[-1] if vals else None,
        "hrv_avg7": round(statistics.mean(vals[-7:]), 1) if vals else None,
        "hrv_avg30": round(statistics.mean(vals), 1) if vals else None,
        "hrv_history": [{"date": r["date"], "value": r["ВСР_ночная"]} for r in history if r["ВСР_ночная"] is not None],
        "resting_hr": rhr_row[0]["Пульс_ночной_средний"] if rhr_row else None,
        "acwr": acwr, "acwr_status": acwr_status,
        "coach": coach,
        "publications": _topic_publications(cur, "recovery"),
    }


def build_sleep_detail(cur, sleep_min_today: Optional[int], sleep_quality_today: Optional[float]) -> dict:
    """Живая поправка Влада (2026-09-28): «должен быть быстрый глубокий и
    РЕМ в сумме с пробуждениями это весь сон» — проверено на реальных
    данных: Легкий_сон_мин + Глубокий_сон_мин + REM_сон_мин = Чистый_сон_мин
    ТОЧНО (28.09: 184+117+135=436), и Время_в_кровати_мин − Бодрствование_
    мин = тот же Чистый_сон_мин. Значит "быстрый" — это Лёгкий сон (третья,
    ранее не показанная стадия), не синоним REM — показываю оба явно, а не
    гадаю дальше. "Качество" (Эффективность_сна_) часто пусто (см. скриншот
    Влада — "—"); Оценка_сна_балл (реальный Гарминовский Sleep Score, теперь
    же красит кругляш «Сон», см. build_chips) есть почти всегда — показываю
    её вместо "Качества", не рядом."""
    history_min = _recent_daily_values(
        cur, ["Чистый_сон_мин", "Легкий_сон_мин", "Глубокий_сон_мин", "REM_сон_мин", "Бодрствование_мин"], 14)
    hours_history = [{"date": r["date"], "hours": round(r["Чистый_сон_мин"] / 60, 2)}
                      for r in history_min if r["Чистый_сон_мин"] is not None]
    minutes = [r["Чистый_сон_мин"] for r in history_min if r["Чистый_сон_мин"] is not None]
    avg14 = round(statistics.mean(minutes)) if minutes else None
    last = history_min[-1] if history_min else {}
    delta = (sleep_min_today - avg14) if (sleep_min_today is not None and avg14) else None
    if delta is None:
        coach = "Пока рано сравнивать со средним — данных за 14 ночей не хватает."
    elif delta >= 15:
        coach = f"Сон длиннее среднего на {delta} мин — хорошая база для серии."
    elif delta <= -15:
        coach = f"Короче среднего на {abs(delta)} мин — сегодня стоит лечь пораньше."
    else:
        coach = "Сон стабилен, в пределах обычного диапазона."
    return {
        "segment": "sleep",
        "last_night_min": sleep_min_today, "avg14_min": avg14, "delta_min": delta,
        "sleep_score": sleep_quality_today,
        "history": hours_history,
        "light_min": last.get("Легкий_сон_мин"), "deep_min": last.get("Глубокий_сон_мин"),
        "rem_min": last.get("REM_сон_мин"), "awake_min": last.get("Бодрствование_мин"),
        "coach": coach,
        "publications": _topic_publications(cur, "sleep"),
    }


def _todays_workouts(cur) -> list[dict]:
    """Тренировки (Тренировка_N_Тип/Мин, N=1-3) — тип текстовый ("Нет" когда
    не было), _recent_daily_values() сюда не годится (она числами через
    _num(), тип потерялся бы). "Нет"/пусто/0 минут — не тренировка, не
    показываем пустые слоты."""
    cur.execute(
        'SELECT "Тренировка_1_Тип","Тренировка_1_Мин","Тренировка_2_Тип","Тренировка_2_Мин",'
        '"Тренировка_3_Тип","Тренировка_3_Мин" FROM health.daily_trends ORDER BY "Дата" DESC LIMIT 1'
    )
    row = cur.fetchone()
    if not row:
        return []
    out = []
    for i in range(0, 6, 2):
        kind, minutes = row[i], _num(row[i + 1])
        if kind and kind.strip() and kind.strip().lower() != "нет" and minutes:
            out.append({"type": kind.strip(), "minutes": minutes})
    return out


def build_move_detail(cur, today: dict, steps: dict, gate: dict) -> dict:
    """Живая жалоба Влада (2026-09-28): «вкладка движение открывает только
    шаги, в макете по-другому было» — добавлены нагрузка (ACWR, была видна
    только в «Заряде», хотя backend с самого начала относит её к сегменту
    move, см. _collect_judgments) и тренировки дня (были не показаны нигде
    в Vita вообще)."""
    # "Шаги_за_вчера" — единственная посуточная история шагов в системе
    # (см. докстринг секции выше: часовой разбивки нет вообще).
    history = [{"date": r["date"], "steps": int(r["Шаги_за_вчера"])}
               for r in _recent_daily_values(cur, ["Шаги_за_вчера"], 7) if r["Шаги_за_вчера"] is not None]
    coach = ("Цель снижена на время щадящего режима: ходьба и плавание, без бега и прыжков."
             if gate.get("blocked") else "Ограничений по нагрузке нет — держи темп.")
    return {
        "segment": "move",
        "steps_now": steps.get("now_steps"), "steps_target": steps.get("target"),
        "status_word": steps.get("status_word"), "behind_pace": steps.get("behind_pace"),
        # acwr/acwr_status — внутри today["decision"], см. докстринг живого
        # бага в build_recovery_detail выше.
        "acwr": (today.get("decision") or {}).get("acwr"),
        "acwr_status": (today.get("decision") or {}).get("acwr_status"),
        "workouts": _todays_workouts(cur),
        "history_daily": history,
        "coach": coach,
        "publications": _topic_publications(cur, "move"),
    }


def build_food_topic_detail(cur) -> dict:
    """Живая поправка Влада (2026-09-28): "тепловая карта уже была реализована
    в предыдущей версии, она на бэкенде есть" — был неправ, заявив, что
    недельной тепловой карты микронутриентов нет вообще: get_weekly_nutrition()
    (app/dashboard.py, порт n8n-кэша "Питание за неделю") уже считает её —
    "heatmap" = нутриенты, где Влад НЕДОБИРАЕТ (avgPct<85% от RDA или 2+ дня
    выше верхнего предела), "только отклонения" уже встроено в сам расчёт
    (переменная deviates), не нужно фильтровать заново. Нутриенты РИСКА
    ИЗБЫТКА (натрий/сахар/жиры) сюда не попадают — они уже отдельно на
    главном экране питания через рычаги/бюджет."""
    from app.dashboard import get_weekly_nutrition

    weekly = get_weekly_nutrition(cur)
    return shape_food_topic(weekly, _yesterday_meals(cur), _topic_publications(cur, "food"))


def _week_top_sources(src: Optional[dict], limit: int = 3) -> list[str]:
    """Сверка с макетом v7: тап по нутриенту — «откуда он брался за неделю».
    Суммирует дневные топ-источники (get_weekly_nutrition.sources[...].byDay) по
    неделе. Персонального AI-совета «чем восполнить» на бэкенде нет —
    показываем честное «откуда был» + справку из nutrient_targets.Примечание."""
    acc: dict[str, float] = {}
    for day in (src or {}).get("byDay") or []:
        for item in day or []:
            acc[item["name"]] = acc.get(item["name"], 0) + (item.get("pct") or 0)
    return [n for n, _ in sorted(acc.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


def shape_food_topic(weekly: dict, yesterday: dict, publications: list[dict]) -> dict:
    sources = weekly.get("sources") or {}
    heatmap = [
        {"label": h["label"], "avg_pct": h["avgPct"], "level": h["level"], "unit": h["unit"],
         "days_pct": h["values"], "note": h.get("note") or None,
         "upper_pct": h.get("upperBoundPct"),  # верхний допустимый предел в % нормы (только для пищи) — выше него клетка красная
         "top_sources": _week_top_sources(sources.get(h["label"]))}
        for h in (weekly.get("heatmap") or [])
    ]
    dq = weekly.get("diet_quality") or {}
    ahei = dq.get("ahei") or {}
    plants = dq.get("plants") or {}
    return {
        "segment": "food",
        "days": weekly.get("days") or [],
        "micro_heatmap": heatmap,
        "normal": [m["label"] for m in (weekly.get("normal") or []) if m.get("label")],
        "diet_quality": None if dq.get("error") or not ahei else {
            "ahei_week": ahei.get("week_avg"), "ahei_target": ahei.get("target"), "ahei_max": ahei.get("max"),
            "plants": plants.get("count"), "plants_target": plants.get("target"),
        },
        "yesterday": yesterday,
        "publications": publications,
    }


def _yesterday_meals(cur) -> dict:
    """Экран «Вчера» из шторки «Питание» (макет v7): приёмы пищи вчерашних суток
    с белком — прямо из health.meals (та же таблица, что _dish_sources)."""
    tz = timeutil.person_tz_name()
    cur.execute(
        'SELECT to_char("Date" AT TIME ZONE %s, \'HH24:MI\'), "Meal_description", "Proteins", "Calories", "Насыщенные жиры" '
        'FROM health.meals WHERE ("Date" AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date - 1 '
        'ORDER BY "Date"',
        (tz, tz, tz),
    )
    meals, sat_fat = [], 0.0
    for t, d, p, k, f in cur.fetchall():
        meals.append({"t": t, "d": (d or "").strip(), "p": round(_num(p) or 0), "k": round(_num(k) or 0)})
        sat_fat += _num(f) or 0
    # «Жиры 24/28 г · серия не прервалась» (макет v7) — насыщенные жиры вчерашних суток
    return {"meals": meals, "protein": sum(m["p"] for m in meals), "kcal": sum(m["k"] for m in meals),
            "sat_fat": round(sat_fat, 1) if meals else None}


# =====================================================================
# CHIP_NORM — пороги, по которым кругляш красится "хорошо"/"можно улучшить"
# (Vita v2, этап 1, Часть «Бэкенд-дельта» п.5). Были захардкожены в мокапе
# (`const CHIP_NORM={recovery:65,sleep:70,move:70,food:70}`), перенесены в
# health.user_profile.vita_chip_norm (migrations/0003_vita_v2.sql) — единственный
# профиль в системе, читаем/пишем как есть, без отдельной card.*-таблицы ради
# одной строки.
# =====================================================================

DEFAULT_CHIP_NORM = {"recovery": 65, "sleep": 70, "move": 70, "food": 70}
_SCORE_KEY_BY_SEGMENT = {"recovery": "recovery_score", "sleep": "sleep_score",
                          "move": "movement_score", "food": "nutrition_score"}


def read_chip_norm(cur) -> dict:
    cur.execute("SELECT vita_chip_norm FROM health.user_profile LIMIT 1")
    row = cur.fetchone()
    if row and row[0]:
        return {**DEFAULT_CHIP_NORM, **row[0]}
    return dict(DEFAULT_CHIP_NORM)


def chip_status(scores: dict, chip_norm: dict, chips: Optional[dict] = None) -> dict:
    """good/warn по сегменту — null, если сегмент вообще не оценивается
    (день без критериев, не "оценка 0", тот же принцип, что _score_from_judgments).
    sleep/recovery красятся по РЕАЛЬНЫМ числам Гармина (chips.sleep_quality/
    energy), если они есть — судейский скор (100 при любом «не плохо») туда
    больше не годится, см. build_chips. move/food остаются на судейском —
    для них нет одного готового композитного числа от часов."""
    chips = chips or {}
    real_by_segment = {"sleep": chips.get("sleep_quality"), "recovery": chips.get("energy")}
    out = {}
    for segment, score_key in _SCORE_KEY_BY_SEGMENT.items():
        v = real_by_segment.get(segment)
        if v is None:
            v = scores.get(score_key)
        norm = chip_norm.get(segment, DEFAULT_CHIP_NORM[segment])
        out[segment] = None if v is None else ("good" if v >= norm else "warn")
    return out


# =====================================================================
# Состояние дня — ФЛАГИ, не тексты (Часть 2 тикета): фронтенд сам решает,
# какими словами макета показать каждую комбинацию.
# =====================================================================

def _time_of_day(now_local) -> str:
    """Пороги — по примерам макета (8:00 утро · 16:02 день · 21:30 вечер),
    не измеренная величина, календарное соглашение.

    Живой баг (2026-09-28, поймал Влад): порог был h<19 — «день закрыт»
    (state.closed) загорался уже в 19:00-19:36, хотя докстринг тут же цитирует
    собственный пример макета "21:30 вечер". Час честно передвинут на границу
    из примера — 21, а не 19 (был опечаткой/недосмотром при первом переносе,
    не намеренным решением)."""
    h = now_local.hour
    if h < 11:
        return "morning"
    if h < 21:
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
    sleep_score_m = metric_by_key.get("sleep_score") or {}
    trend_word = None
    if hrv.get("judgment") == "good":
        trend_word = "растёт"
    elif hrv.get("judgment") == "bad":
        trend_word = "ниже базы"
    protein_consumed = _num((tn.get("summary") or {}).get("macros", {}).get("proteins", {}).get("consumed"))
    food_logged = (today.get("meals_today") or 0) > 0
    return {
        "sleep_min": sleep_m.get("value"),
        # Живая жалоба Влада (2026-09-28): кругляш «Сон» показывал 100 для
        # ЛЮБОЙ ночи без bad/warn (внутренняя формула штрафов, не реальное
        # число) — "7:16 это 100, а 8:00 будет 120?" тот же вопрос и про
        # «Заряд». sleep_quality — РЕАЛЬНАЯ оценка сна Гармина (Оценка_сна_
        # балл, уже была в metric_coverage/METRIC_CONFIG, просто не пробрасывалась
        # сюда) — кругляш «Сон» теперь красится и считается по ней, energy
        # (Body Battery, уже была) — по ней же для «Заряд».
        "sleep_quality": sleep_score_m.get("value"),
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
        return {"tone": "neutral", "text": "Часы не видели тебя с утра — надеть?", "go": "watch", "short": "Надень часы"}
    if state["no_food"]:
        return {"tone": "neutral", "text": "Ждёт первого приёма — сфотографировать в дневнике.", "go": "protein",
                "short": "Запиши первый приём"}
    if state["time_of_day"] == "evening":
        return None  # день закрыт — подсказывать поздно, коуч ниже уже сказал итог
    left = None
    if protein.get("target") is not None and protein.get("consumed") is not None:
        left = protein["target"] - protein["consumed"]
    behind_pace = steps.get("behind_pace")
    if behind_pace and left and left > 20:
        return {"tone": "apricot",
                "text": f"Прогулка после ужина поможет с темпом. Белок {round(protein['consumed'])}/{round(protein['target'])} — творог вечером.",
                "go": "protein", "short": "Прогулка и творог вечером"}
    if left and left > 20:
        return {"tone": "apricot", "text": f"Белок {round(protein['consumed'])}/{round(protein['target'])} — ещё один приём с творогом или курицей закроет цель.", "go": "protein",
                "short": "Творог или курица на ужин"}
    if behind_pace:
        return {"tone": "apricot", "text": "Темп шагов чуть ниже обычного — короткая прогулка выправит день.", "go": "steps",
                "short": "Короткая прогулка"}
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
    # Сверка «Питания» с макетом v7 (2026-09-29): в макете лимиты — клинически
    # значимые пределы; сахар в том же _CLINICAL_LIMITS и в сериях, но рычагом
    # не был — «Лимиты» показывали только два из трёх.
    "sugar": {"label": "Добавленный сахар", "budget_label": "Добавленный сахар", "col": "Добавленный сахар", "streak_label": "Сахар в норме"},
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
# Серии — история по дням (Vita v2, этап 1, Часть «Бэкенд-дельта» п.3):
# вехи 7/14/30/60/90, рекорд = max по ВСЕЙ истории (не только текущая серия),
# atRisk = критерий сегодня у края. Считается на лету из уже существующих
# health.daily_trends/health.meals — не заводим отдельную таблицу истории,
# та же логика, что app.outcomes_report (тикет «хвост») — данные и так
# полностью нормализованы, материализация не нужна на этом объёме.
#
# Заморозки — "одна копилка на все серии" (Часть 1 плана сборки макета,
# п.10/118): упрощение — раздаются самым СВЕЖИМ пропускам по датам первыми
# (across ВСЕХ серий одновременно, не по одной), не по отдельному пулу на
# каждую серию. Рекорд считается БЕЗ заморозок (иначе прошлые рекорды
# задним числом "подрастали" бы от новой механики, которой тогда не было).
# =====================================================================

STREAK_FREEZE_POOL = 2  # заморозок в копилке — то же малое число, что в фитнес-приложениях (Duolingo и т.п.), не измерено отдельно
STREAK_MILESTONES = [7, 14, 30, 60, 90]
STREAK_STRIP_DAYS = 14  # полоса дней в шторке «Серии» (макет v7)

_STREAK_CRITERIA = [
    {"key": "sleep_zone", "label": "Сон в зоне 7–9 ч"},
    {"key": "Натрий", "label": "Соль в норме"},
    {"key": "Добавленный сахар", "label": "Сахар в норме"},
    {"key": "Насыщенные жиры", "label": "Жиры в норме"},
]


# =====================================================================
# Реконструкция "{today, health} для произвольного дня истории" — общая
# для калибровки ring.ahead (app/vita_calibration.py) и снимка дня
# (write_day_snapshot ниже, Часть «Бэкенд-дельта» п.4). Один источник
# правды на "что мы вообще можем честно узнать про прошлый день" — не
# второй набор порогов на второй лад.
#
# Две формы истории нужны параллельно (наследие двух разных функций
# dashboard.py, которые исторически читали health.daily_trends по-разному
# — не унифицировано в этом тикете, не тот объём): _baseline/_load_mean
# (для decision.reasons) ждут list[dict] с "Дата" СТРОКОЙ внутри каждого
# словаря; _baseline_for/_baseline_metrics_for_index (для health.metrics)
# ждут list[tuple[date, dict]] с датой ОБЪЕКТОМ на первом месте.
# =====================================================================

_DAILY_TRENDS_COLS = [
    "Дата", "Восстановление_BodyBattery", "ВСР_ночная", "ACWR_Garmin", "ACWR_Status",
    "Тренировка_Ккал", "Чистый_сон_мин", "Оценка_сна_балл", "Эффективность_сна_",
    "Стресс_дневной_средний", "Шаги_за_вчера", "Пульс_ночной_средний", "VO2_Max",
]


def _fetch_history(cur):
    cols = ", ".join(f'"{c}"' for c in _DAILY_TRENDS_COLS)
    cur.execute(f'SELECT {cols} FROM health.daily_trends ORDER BY "Дата" ASC')
    raw = [dict(zip(_DAILY_TRENDS_COLS, r)) for r in cur.fetchall()]
    raw = [r for r in raw if r["Дата"]]

    # _baseline_for/_baseline_metrics_for_index НЕ конвертируют значения сами —
    # ждут уже готовые числа в rows[i][1][col], ровно как get_health_dashboard
    # строит их через _num() при чтении. health.* колонки — TEXT (наследие
    # Sheets), сырые значения из курсора — строки.
    rows_tuple = [(r["Дата"], {k: _num(v) for k, v in r.items() if k != "Дата"}) for r in raw]
    # _baseline/_load_mean, наоборот, сами вызывают _num() на каждое значение —
    # им годится и сырая строка.
    rows_flat = [{**r, "Дата": r["Дата"].isoformat()} for r in raw]

    from psycopg.rows import dict_row
    cur.execute(
        'SELECT "Date", "Насыщенные жиры", "Натрий", "Добавленный сахар" '
        'FROM health.meals WHERE "Date" IS NOT NULL'
    )
    meals = [{"Date": d.isoformat() if hasattr(d, "isoformat") else str(d),
              "Насыщенные жиры": fat, "Натрий": na, "Добавленный сахар": sugar}
             for d, fat, na, sugar in cur.fetchall()]

    targets_cur = cur.connection.cursor(row_factory=dict_row)
    targets_cur.execute('SELECT * FROM health.nutrient_targets')
    targets = targets_cur.fetchall()

    return rows_tuple, rows_flat, meals, targets


def _historical_day_dicts(rows_tuple: list, rows_flat: list[dict], idx: int,
                           meals: list[dict], targets: list[dict]) -> tuple[dict, dict]:
    """{today, health}-совместимые словари для дня rows_tuple[idx], теми же
    формулами, что get_today_dashboard/get_health_dashboard, для
    произвольного индекса истории, не только последнего дня."""
    day, row = rows_tuple[idx]
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]

    bb = _num(row.get("Восстановление_BodyBattery"))
    hrv = _num(row.get("ВСР_ночная"))
    hrv_base = _baseline(rows_flat, "ВСР_ночная", day_iso, 30)
    hrv_delta = (hrv - hrv_base) if (hrv is not None and hrv_base is not None) else None

    acwr_garmin = _num(row.get("ACWR_Garmin"))
    acwr_status_g = (str(row.get("ACWR_Status") or "").strip().upper()) or None
    if acwr_garmin is not None:
        acwr, acwr_source = acwr_garmin, "garmin"
    else:
        acute, chronic = _load_mean(rows_flat, day_iso, 7), _load_mean(rows_flat, day_iso, 28)
        acwr = round((acute / chronic) * 100) / 100 if (acute is not None and chronic) else None
        acwr_source = "self" if acwr is not None else None
    load_high = (acwr_status_g == "HIGH") if acwr_status_g else (acwr is not None and acwr > 1.5)

    reasons = _build_reasons(bb, hrv_delta, acwr, acwr_status_g, acwr_source, load_high)
    metric_by_key = _baseline_metrics_for_index(rows_tuple, idx)
    budget = _budget_for_day(meals, targets, day_iso)

    today = {"decision": {"reasons": reasons}, "budget": budget}
    health = {"metrics": list(metric_by_key.values())}
    return today, health


# =====================================================================
# Снимок дня (Vita v2, этап 1, Часть «Бэкенд-дельта» п.4) — денормализованный
# слепок ключевых полей для экрана «вчера», ТОЛЬКО ЧТЕНИЕ. Пишется РОВНО
# один раз в день, из finalize_yesterday() (app/nutrition_reports.py), КОГДА
# вчера уже точно закрыто. Честно: не полная копия build_today() — steps
# (health.live_steps_today) не хранит историю вообще (garminbot перезаписывает
# одну строку "на сейчас"), нудж/рычаги завязаны на ЖИВОЕ состояние ("сейчас
# вечер" и т.п.), которого у прошлого дня по определению нет. Снимок несёт
# то, что можно честно восстановить из card/health-истории: скор+сегменты,
# chip_status, ключевые значения чипов (сон/ВСР/энергия), вердикт/гейт.
# =====================================================================

def write_day_snapshot(cur, day) -> bool:
    """True — снимок записан, False — на этот день нет строки в
    health.daily_trends (нечего снимать, например самый первый день истории)."""
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    rows_tuple, rows_flat, meals, targets = _fetch_history(cur)
    idx = next((i for i, (d, _r) in enumerate(rows_tuple) if d.isoformat() == day_iso), None)
    if idx is None:
        return False

    today_d, health_d = _historical_day_dicts(rows_tuple, rows_flat, idx, meals, targets)
    scores = _scores(today_d, health_d)
    chip_norm = read_chip_norm(cur)
    metric_by_key = {m["key"]: m for m in health_d["metrics"]}
    gate = build_gate(today_d)

    chips = {
        "sleep_min": (metric_by_key.get("sleep_min") or {}).get("value"),
        "sleep_quality": (metric_by_key.get("sleep_score") or {}).get("value"),
        "hrv": {"value": (metric_by_key.get("hrv") or {}).get("value")},
        "energy": (metric_by_key.get("body_battery") or {}).get("value"),
    }

    day_index = _day_index(scores, chips)
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (date, ring, chips, gate, streaks) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (date) DO UPDATE SET ring = EXCLUDED.ring, chips = EXCLUDED.chips, "
            "gate = EXCLUDED.gate, streaks = EXCLUDED.streaks, ts_recorded = now()"
        ).format(t=sql.Identifier(schema(), "vita_day_snapshot")),
        (day_iso, json.dumps({**scores, "score": day_index, "chip_status": chip_status(scores, chip_norm, chips)},
                              ensure_ascii=False),
         json.dumps(chips, ensure_ascii=False), json.dumps(gate, ensure_ascii=False), None),
    )
    return True


def read_day_snapshot(cur, day) -> Optional[dict]:
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    cur.execute(
        sql.SQL("SELECT date, ring, chips, gate FROM {t} WHERE date = %s")
        .format(t=sql.Identifier(schema(), "vita_day_snapshot")),
        (day_iso,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    d, ring, chips, gate = row
    return {"date": d.isoformat(), "ring": ring, "chips": chips, "gate": gate}


# =====================================================================
# Назначения врача — ручные отметки (Vita v2, этап 1, Часть «Бэкенд-дельта»
# п.6): «Плавание_было»/«Провал_без_движения_мин ≤ 40»/«шаги ≥ цели» обычно
# приходят из Гармина (health.daily_trends) — когда часы не видели (no_watch),
# Влад может отметить их руками. source в ответе честно называет, откуда
# взято значение конкретного дня — фронтенд не должен путать "сказал сам" с
# "увидели часы".
# =====================================================================

MANUAL_MARK_FIELDS = ("swim_happened", "movement_ok", "steps_target_met")


def write_manual_mark(cur, day, field_key: str, value: bool) -> None:
    if field_key not in MANUAL_MARK_FIELDS:
        raise ValueError(f"неизвестное поле ручной отметки: {field_key!r}")
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (date, field_key, value_bool) VALUES (%s, %s, %s) "
            "ON CONFLICT (date, field_key) DO UPDATE SET value_bool = EXCLUDED.value_bool, ts_recorded = now()"
        ).format(t=sql.Identifier(schema(), "vita_manual_mark")),
        (day_iso, field_key, value),
    )


def _read_manual_marks(cur, day_iso: str) -> dict:
    cur.execute(
        sql.SQL("SELECT field_key, value_bool FROM {t} WHERE date = %s")
        .format(t=sql.Identifier(schema(), "vita_manual_mark")),
        (day_iso,),
    )
    return dict(cur.fetchall())


def build_assignments(cur, today: dict, state: dict) -> dict:
    """Часть «Бэкенд-дельта» п.6 — узко про ручной ввод, когда часы не
    видели. Полное отображение гарминовской стороны "Назначений врача"
    (Плавание_было/Провал_без_движения_мин/шаги за день) — отдельная задача
    показа существующих health.daily_trends полей, не введена этим тикетом
    (в v1 её не было вовсе, здесь — только сама возможность ручной отметки,
    честно ограничено). Когда часы видели день — просто говорим об этом,
    не подменяя Гармин выдуманными полями, которых не собирали."""
    if not state.get("no_watch"):
        return {"source": "garmin", "swim_happened": None, "movement_ok": None, "steps_target_met": None}
    day_iso = today.get("date") or timeutil.now_local().date().isoformat()
    marks = _read_manual_marks(cur, day_iso)
    return {
        "source": "manual" if marks else None,
        "swim_happened": marks.get("swim_happened"),
        "movement_ok": marks.get("movement_ok"),
        "steps_target_met": marks.get("steps_target_met"),
    }


def _day_met_and_at_risk(day_iso: str, row: dict, meals: list[dict], targets: list[dict], key: str):
    """(met, at_risk, has_data) для одного критерия на один день. has_data
    отличает "не выполнено" от "данных не было вообще" (день без Гармина/без
    еды в дневнике) — дни без данных не засчитываются НИ поломкой, НИ
    выполнением (пропускаются в подсчёте серии, как отсутствие информации,
    не как провал)."""
    if key == "sleep_zone":
        v = _num(row.get("Чистый_сон_мин"))
        if v is None:
            return None, False, False
        met = _SLEEP_MIN_OK <= v <= _SLEEP_MAX_OK
        at_risk = met and (v - _SLEEP_MIN_OK <= 15 or _SLEEP_MAX_OK - v <= 15)
        return met, at_risk, True
    budget = _budget_for_day(meals, targets, day_iso)
    b = next((x for x in budget if x["label"] == key), None)
    if b is None:
        return None, False, False
    met = b["status"] != "over"
    at_risk = met and b["status"] == "close"
    return met, at_risk, True


def _fetch_streak_inputs(cur):
    """rows/meals/targets на ВСЮ историю (не 3-дневное окно get_today_dashboard —
    тому окну хватает для текущего бюджета дня, сериям нужны месяцы). Тот же
    паттерн запроса, что get_today_dashboard, просто без узкого фильтра на meals."""
    tz = timeutil.person_tz_name()
    cur.execute('SELECT d.*, to_char(d."Дата", \'YYYY-MM-DD\') AS "Дата" FROM health.daily_trends d ORDER BY d."Дата"')
    rows = _rows_as_dicts(cur)
    cur.execute(
        "SELECT m.*, to_char(m.\"Date\" AT TIME ZONE %s, 'YYYY-MM-DD\"T\"HH24:MI') AS \"Date\" "
        'FROM health.meals m ORDER BY m."Date"', (tz,)
    )
    meals = _rows_as_dicts(cur)
    cur.execute('SELECT * FROM health.nutrient_targets')
    targets = _rows_as_dicts(cur)
    return rows, meals, targets


def build_streaks(rows: list[dict], meals: list[dict], targets: list[dict], today_iso: str) -> list[dict]:
    days = sorted(r["Дата"] for r in rows if r.get("Дата") and r["Дата"] <= today_iso)
    rows_by_date = {r["Дата"]: r for r in rows}

    # Собираем пропуски (met is False, не None) по ВСЕМ критериям вместе,
    # отсортированные по дате УБЫВАЮЩЕ — свежие пропуски получают заморозку
    # первыми (общая копилка, не по сериям).
    per_criterion_series: dict[str, list] = {}
    all_gaps = []  # (date, criterion_key) — только настоящие провалы, не "нет данных"
    for crit in _STREAK_CRITERIA:
        series = []
        for d in days:
            met, at_risk, has_data = _day_met_and_at_risk(d, rows_by_date[d], meals, targets, crit["key"])
            series.append({"date": d, "met": met, "at_risk": at_risk, "has_data": has_data})
            if has_data and met is False:
                all_gaps.append((d, crit["key"]))
        per_criterion_series[crit["key"]] = series
    all_gaps.sort(key=lambda g: g[0], reverse=True)
    frozen = {g for g in all_gaps[:STREAK_FREEZE_POOL]}
    freezes_used = len(frozen)

    out = []
    for crit in _STREAK_CRITERIA:
        series = per_criterion_series[crit["key"]]
        # рекорд — макс. подряд идущих met=True, БЕЗ заморозок, дни без данных
        # серию не рвут и не продлевают (нейтральны)
        record = cur_run = 0
        for day in series:
            if day["met"] is True:
                cur_run += 1
                record = max(record, cur_run)
            elif day["met"] is False:
                cur_run = 0
        # текущая серия — с конца, с учётом заморозок (только для этого расчёта)
        current = 0
        for day in reversed(series):
            if day["met"] is True:
                current += 1
            elif day["met"] is False and (day["date"], crit["key"]) in frozen:
                current += 1  # заморожен — серия не рвётся
            elif day["met"] is False:
                break
            # met is None (нет данных) — пропускаем день, не рвём и не продлеваем
        today_status = series[-1] if series else None
        next_milestone = next((m for m in STREAK_MILESTONES if m > current), None)
        # Полоса последних STREAK_STRIP_DAYS дней (макет v7, шторка «Серии»):
        # y — выполнено, f — провал закрыт заморозкой, x — провал, p — нет
        # данных (пауза), t — сегодня ещё идёт, r — сегодня под угрозой.
        strip = []
        for day in series[-STREAK_STRIP_DAYS:]:
            if day["date"] == today_iso:
                code = "r" if day["at_risk"] else ("p" if not day["has_data"] else "t")
            elif day["met"] is True:
                code = "y"
            elif day["met"] is False:
                code = "f" if (day["date"], crit["key"]) in frozen else "x"
            else:
                code = "p"
            strip.append({"date": day["date"], "c": code})
        out.append({
            "key": crit["key"], "label": crit["label"], "count": current, "record": record,
            "next_milestone": next_milestone, "days": strip,
            "at_risk": bool(today_status and today_status["at_risk"]),
            "status": "paused" if not (today_status and today_status["has_data"]) else ("at_risk" if today_status["at_risk"] else "alive"),
        })
    return {"streaks": [s for s in out if s["count"] > 0 or s["record"] > 0], "freezes_available": STREAK_FREEZE_POOL - freezes_used}


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
    chips = build_chips(today, health, tn)
    day_index = _day_index(scores, chips)
    ahead = compute_ahead(today, health, state, steps, protein, chips)
    longevity = today.get("longevity") or {}
    bioage_days = round((longevity.get("affects_today_total") or 0) * 365, 1) if longevity else None
    chip_norm = read_chip_norm(cur)

    rows, meals, targets = _fetch_streak_inputs(cur)
    streak_data = build_streaks(rows, meals, targets, today.get("date") or timeutil.now_local().date().isoformat())

    return {
        "date": today.get("date"),
        "state": state,
        "gate": gate,
        # ring.score: живая правка Влада (2026-09-28) — индекс дня обязан
        # зависеть от кругляшей (см. _day_index), не от параллельной
        # судейской системы. **scores несёт остальные *_score поля как были
        # (нужны chip_status/streaks/calibration), "score" явно переопределён
        # после спреда.
        # ring.ahead: калибровка на истории (app/vita_calibration.py, 2026-09-28)
        # дала mean_abs_error=18.5 на n=2 — выше порога в 5. По решению Влада
        # показываем ahead всё равно, но с честной пометкой "оценочно"
        # (ahead_confidence) — фронтенд обязан её показать рядом с числом,
        # не выдавать за точный прогноз.
        "ring": {**scores, "score": day_index, "ahead": ahead, "ahead_confidence": "estimated",
                 "bioage_days": bioage_days, "breakdown": build_index_breakdown(today, health)},
        "chips": chips,
        "chip_status": chip_status(scores, chip_norm, chips),
        "nudge": nudge,
        "streaks": streak_data["streaks"],
        "freezes_available": streak_data["freezes_available"],
        "assignments": build_assignments(cur, today, state),
        # «Проверки», этап 2 (2026-09-28): слот «Решить» (этап 1 оставил его
        # пустым — см. app/static/vita.html до этого коммита) + строка
        # «N проверок идут · ближайший вердикт» (Часть 3 тикета).
        "inbox": checks.home_inbox(cur),
        "checks_summary": checks.checks_summary(cur),
        # Живая жалоба (2026-09-28): точка на вкладке «Проверки» должна
        # гореть при ЛЮБОМ нерешённом вопросе, не только свежих из inbox
        # (разногласия консилиума там никогда не появляются, см. checks.py).
        "pending_questions": checks.pending_questions_count(cur),
        # Точка на вкладке «Врач»: ближайшая панель сдачи уже сегодня (или просрочена).
        "lab_draw_due": lab_draw_due(cur),
        # Неделя — заголовок главной по воскресеньям (макет v7).
        "week": build_week(cur, today.get("date") or timeutil.now_local().date().isoformat()),
    }


# =====================================================================
# Неделя (макет v7: по воскресеньям заголовок главной — «5 из 7 дней в зелёной
# зоне»). Источник — card.vita_day_snapshot (пишет finalize_yesterday каждую
# ночь), ничего не пересчитывается задним числом. «Зелёный» день — индекс от
# WEEK_GREEN_SCORE, тот же порог, что у короны на дуге (правка Влада 28.09).
# =====================================================================

WEEK_GREEN_SCORE = 80


def summarize_week(snapshots: list[tuple]) -> Optional[dict]:
    """snapshots — [(date, ring_dict)] за последние 7 закрытых суток."""
    scored = [(d, (ring or {}).get("score")) for d, ring in snapshots]
    scored = [(d, s) for d, s in scored if s is not None]
    if not scored:
        return None
    green = sum(1 for _, s in scored if s >= WEEK_GREEN_SCORE)
    return {
        "days": len(scored), "green": green, "avg": round(statistics.mean(s for _, s in scored)),
        "scores": [{"date": d.isoformat() if hasattr(d, "isoformat") else str(d), "score": s} for d, s in scored],
    }


def build_week(cur, today_iso: str) -> Optional[dict]:
    cur.execute(
        sql.SQL("SELECT date, ring FROM {t} WHERE date < %s::date AND date >= %s::date - 7 ORDER BY date")
        .format(t=sql.Identifier(schema(), "vita_day_snapshot")),
        (today_iso, today_iso),
    )
    return summarize_week(cur.fetchall())


# =====================================================================
# «Врач» (макет v7): три строки — консилиум · заключение врача · медпаспорт —
# и одна карточка ближайшей сдачи. Новых источников нет: консилиум —
# consilium.get_consilium_reports, заметки врача и лабы вне референса — те же
# частные функции, что читает досье (app.doctor.context), сдачи —
# lab_optimizer.generate_plan. shape_doctor — чистая, тестируется без базы.
# =====================================================================

def _marker_group(source_type: str, code: str) -> str:
    """Группа в шторке «Сдача» — КТО назначил, а не система организма
    (просьба Влада, 29.09: «какие анализы входят в панель PhenoAge, а какие
    назначил ИИ врач; группировать по органам не надо»)."""
    if source_type == "standing":
        from app.lab_catalog import PHENOAGE_PANEL_MARKERS
        return "Панель PhenoAge" if code in PHENOAGE_PANEL_MARKERS else "Плановый мониторинг"
    if source_type == "intervention_monitor":
        return "Контроль добавки"
    if source_type in ("manual", "recommendation"):
        return "Назначил врач"
    return "По показаниям"


_MONTH_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне",
               "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]
_MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_GROUP_ORDER = ["Панель PhenoAge", "Назначил врач", "Контроль добавки", "Плановый мониторинг", "По показаниям"]


def _iv_label(interval_days, one_time: bool) -> str:
    """Срок повтора анализа для строки «Состав» (макет v5): «90 дн», «6 мес», «1 год», «однократно»."""
    if one_time:
        return "однократно"
    if not interval_days:
        return ""
    d = int(interval_days)
    if d % 365 == 0:
        y = d // 365
        return "1 год" if y == 1 else f"{y} {'года' if 2 <= y <= 4 else 'лет'}"
    if d >= 180 and d % 30 == 0:
        return f"{d // 30} мес"
    return f"{d} дн"


def _marker_reason(code: str, why: str) -> str:
    """Причина назначения из поля why движка («purpose — reason»); у планового — пусто."""
    from app.lab_catalog import LAB_CATALOG
    purpose = (LAB_CATALOG.get(code) or {}).get("purpose") or ""
    if purpose and why.startswith(purpose + " — "):
        return why[len(purpose) + 3:]
    return "" if why == purpose else why


def _draw_groups(markers: list[dict], panel_date: str) -> list[dict]:
    """Блок «Зачем» и группы в «Составе» (макет v5): по КТО назначил, у группы —
    короткий повод (sh) и фраза «зачем» (why); порядок фиксированный."""
    from datetime import date as _date
    pd = _date.fromisoformat(panel_date)
    by: dict[str, list[dict]] = {}
    for m in markers:
        by.setdefault(m["group"], []).append(m)
    out = []
    for name in sorted(by, key=lambda g: _GROUP_ORDER.index(g) if g in _GROUP_ORDER else 99):
        ms = by[name]
        dues = [m["due"] for m in ms if m.get("due")]
        earliest = _date.fromisoformat(min(dues)) if dues else None
        ivs = sorted({m["iv"] for m in ms if m.get("iv") and m["iv"] != "однократно"})
        reasons = []
        for m in ms:
            r = m.get("reason")
            if r and r not in reasons:
                reasons.append(r)
        if name == "Панель PhenoAge":
            due_txt = "пришёл срок" if earliest and earliest <= pd else "по графику"
            sh = f"{due_txt} · раз в {ivs[0]}" if len(ivs) == 1 else due_txt
            why = "все девять сдаются одним забором — без этого биовозраст не пересчитать"
        elif name == "Плановый мониторинг":
            old = earliest and (pd - earliest).days >= 45
            sh = f"срок был в {_MONTH_PREP[earliest.month - 1]} {earliest.year}" if old else "по графику"
            why = "плановые проверки по расписанию каталога"
        elif name in ("Назначил врач", "Контроль добавки"):
            sh = "; ".join(reasons[:2])[:70] if reasons else "по назначению"
            why = "; ".join(reasons)[:220] if reasons else ""
        else:
            sh, why = "по показаниям", ""
        out.append({"name": name, "sh": sh, "why": why, "n": len(ms)})
    return out


def _next_note(panels: list[dict], idx: int) -> str:
    """Строка внизу шторки «Сдача» (макет v5): что дальше по плану."""
    from datetime import date as _date
    if idx + 1 < len(panels):
        d = _date.fromisoformat(panels[idx + 1]["date"])
        when = f"{d.day} {_MONTH_GEN[d.month - 1]}"
        return (f"Больше ничего не нужно. Следующая плановая сдача — не раньше {when}." if idx == 0
                else f"Следующая — {when}.")
    return "После неё в плане на ближайшие полгода ничего нет."


def draw_due(plan: dict, today_iso: str) -> bool:
    """Ближайшая панель сдачи наступила (дата <= сегодня): движок ставит просроченное
    на «сегодня», поэтому точка горит, пока есть что сдавать сегодня."""
    panels = plan.get("panels") or []
    return bool(panels) and panels[0]["date"] <= today_iso


def lab_draw_due(cur) -> bool:
    from app import lab_optimizer
    today = timeutil.now_local().date()
    try:
        return draw_due(lab_optimizer.generate_plan(cur, today=today), today.isoformat())
    except Exception:  # noqa: BLE001 — точка не должна ронять главную
        logger.exception("lab_draw_due failed")
        return False


def shape_doctor(reports: list[dict], notes: list[dict], labs_flags: list[dict], plan: dict,
                 panel: int = 0) -> dict:
    latest = next((r for r in reports if r.get("status") == "completed"), None)
    consilium_block = None
    if latest:
        consilium_block = {
            "id": latest["id"], "date": (latest.get("ts_recorded") or "")[:10], "topic": latest.get("topic"),
            "question": latest.get("question"), "roles": latest.get("roles") or [],
            "actions": [{"text": a.get("imperative"), "accepted": a.get("accepted")}
                        for a in (latest.get("actions") or []) if a.get("imperative")],
            "emerging": [{"method": e.get("method"), "maturity": e.get("maturity"), "grade": e.get("grade")}
                         for e in (latest.get("emerging") or []) if e.get("method")],
            "skeptic": list(latest.get("skeptic_notes") or []),
        }
    panels = plan.get("panels") or []
    next_draw = None
    p = panels[panel] if 0 <= panel < len(panels) else (panels[0] if panels else None)
    if p:
        pick = p.get("pick") or {}  # цены лаб (этап 1 плана docs/PRICES_PLAN_QWEN.md)
        from app.lab_catalog import LAB_CATALOG
        markers_out = []
        for m in p["markers"]:
            code = m.get("code") or ""
            entry = LAB_CATALOG.get(code) or {}
            why = m.get("why") or ""
            markers_out.append({
                "name": m["name"], "why": why, "category": m.get("category"),
                "group": _marker_group(m.get("source_type") or "", code),
                "fasting": m.get("fasting_required"), "source": m.get("source_type"),
                "reason": _marker_reason(code, why),
                "purpose": entry.get("purpose") or "",
                "iv": _iv_label(entry.get("default_interval_days"), bool(entry.get("one_time"))),
                "due": m.get("natural_due_date"),
            })
        next_draw = {
            "date": p["date"], "n": p["n_markers"], "fasting": p["fasting_required"], "tubes": p["tube_types"],
            "price_rub": pick.get("price_rub") or p.get("total_price_rub"),
            "lab_name": pick.get("name"), "labs": p.get("labs") or [], "pick": pick or None,
            "export_text": p.get("export_text"),
            "markers": markers_out,
            "groups": _draw_groups(markers_out, p["date"]),
            "next_note": _next_note(panels, panels.index(p)),
            "shifted": p.get("shifted") or [],
        }
    return {
        "consilium": consilium_block, "consilium_count": sum(1 for r in reports if r.get("status") == "completed"),
        "notes": [{"date": str(n.get("date"))[:10], "category": n.get("category"), "note": n.get("note")} for n in notes],
        "labs_flags": labs_flags,
        "next_draw": next_draw,
        "draws": [{"date": p["date"], "n": p["n_markers"]} for p in panels[:4]],
        "conflicts": plan.get("conflicts") or [],
    }


def build_doctor(cur, lab=None, panel: int = 0) -> dict:
    from app import consilium, lab_optimizer, lab_prices
    from app.doctor.context import _labs_out_of_range, _recent_doctor_notes
    reports = consilium.get_consilium_reports(cur, limit=5)["reports"]
    plan = lab_optimizer.generate_plan(cur)
    lab_prices.attach(cur, plan, chosen_lab=lab)  # цены поверх готового состава — даты/пробирки не трогает
    return shape_doctor(reports, _recent_doctor_notes(cur, limit=3), _labs_out_of_range(cur, limit=10),
                        plan, panel=panel)


# =====================================================================
# «Я» (макет v7): паспорт и биовозраст рядом, лесенка PhenoAge, «что дало» —
# вклад каждого маркера (drivers — те же contributions из health.phenoage_log,
# что уже считает get_bioage_dashboard), системы органов — маркеры по группам.
# Образ жизни по факторам за 30 дней НЕ показывается: bioage_days по дням и
# факторам не хранится (ПЛАН СБОРКИ п.17, «НОВОЕ») — есть только сегодняшний.
# =====================================================================

def shape_me(bio: dict) -> dict:
    pa = bio.get("phenoage") or {}
    drivers = [d for d in (bio.get("drivers") or []) if d.get("type") != "total" and d.get("years") is not None]
    drivers.sort(key=lambda d: -abs(d["years"]))
    groups: dict[str, dict] = {}
    for m in bio.get("biomarkers") or []:
        if m.get("value") is None:
            continue
        g = groups.setdefault(m.get("group") or "Прочее", {"name": m.get("group") or "Прочее", "n": 0, "out": [], "watch": []})
        g["n"] += 1
        if m.get("in_lab_range") is False:
            g["out"].append(m["label"])
        elif m.get("in_opt_range") is False:
            g["watch"].append(m["label"])
    systems = []
    for g in groups.values():
        status = "out" if g["out"] else ("watch" if g["watch"] else "ok")
        systems.append({"name": g["name"], "n": g["n"], "status": status, "flagged": g["out"] or g["watch"]})
    systems.sort(key=lambda g: ({"out": 0, "watch": 1, "ok": 2}[g["status"]], g["name"]))
    return {
        "phenoage": {"value": pa.get("value"), "chrono_age": pa.get("chrono_age"), "delta": pa.get("delta"),
                     "date": pa.get("date"), "note": pa.get("note")},
        "history": [{"date": h["date"], "phenoage": h["phenoage"], "chrono_age": h.get("chrono_age"), "kind": h.get("kind")}
                    for h in (bio.get("history") or []) if h.get("phenoage") is not None],
        "drivers": [{"label": d["label"], "years": d["years"], "value": d.get("value")} for d in drivers],
        "systems": systems,
        "out_of_range": bio.get("out_of_range") or [],
        "data_note": bio.get("data_note"),
    }


def build_me(cur) -> dict:
    from app.dashboard import get_bioage_dashboard
    return shape_me(get_bioage_dashboard(cur))


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
        "macros": build_macros(tn, (today.get("meals_today") or 0) > 0),
    }


def build_macros(tn: dict, food_logged: bool) -> Optional[dict]:
    """Плитки «За день» (макет v7, шторка «Питание»): съедено/цель по ккал и БЖУ —
    те же числа, что уже считает get_today_nutrition (summary). Еды нет — None
    (фронтенд пишет «появятся после первого приёма», не рисует нули)."""
    if not food_logged:
        return None
    summ = tn.get("summary") or {}
    macros = summ.get("macros") or {}
    def pair(d):
        d = d or {}
        return {"consumed": _num(d.get("consumed")), "target": _num(d.get("target"))}
    return {"kcal": pair(summ.get("calories")), "protein": pair(macros.get("proteins")),
            "fat": pair(macros.get("fats")), "carbs": pair(macros.get("carbs"))}


# =====================================================================
# Роуты — вход по паролю (httpOnly cookie, НЕ токен в URL) + сама страница
# и её два JSON-эндпоинта, всё за одной cookie-проверкой (Часть 1 тикета).
# =====================================================================

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

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


@router.get("/vita/yesterday")
def vita_yesterday_endpoint(_: None = Depends(require_session)) -> dict:
    """Экран «вчера» — только чтение снимка, записанного finalize_yesterday()
    (Часть «Бэкенд-дельта» п.4). Нет снимка (например, самый первый день
    истории системы) — честный 404, не выдуманный пустой день."""
    yesterday = timeutil.today() - timedelta(days=1)
    with get_conn() as conn:
        with conn.cursor() as cur:
            snap = read_day_snapshot(cur, yesterday)
    if snap is None:
        raise HTTPException(status_code=404, detail="снимок вчерашнего дня не найден")
    return snap


class VitaManualMarkRequest(BaseModel):
    date: str
    field_key: str
    value: bool


@router.post("/vita/manual-mark")
def vita_manual_mark_endpoint(req: VitaManualMarkRequest, _: None = Depends(require_session)) -> dict:
    """Часть «Бэкенд-дельта» п.6 — Влад отмечает руками то, что обычно видит
    Гармин, когда часы не видели день (source='manual' в /vita/today::assignments)."""
    if req.field_key not in MANUAL_MARK_FIELDS:
        raise HTTPException(status_code=400, detail=f"неизвестное поле: {req.field_key}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_manual_mark(cur, req.date, req.field_key, req.value)
        conn.commit()
    return {"ok": True}


# =====================================================================
# Vita v2, этап 2 (2026-09-28) — «Проверки»: агрегатор app/checks.py
# =====================================================================

@router.get("/vita/checks")
def vita_checks_endpoint(mode: Optional[str] = None, _: None = Depends(require_session)) -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                return checks.list_checks(cur, mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class VitaQuestionResolveRequest(BaseModel):
    question_id: str
    source: str  # 'detective' | 'disagreement'
    action: str  # 'check' | 'decline'
    title: str
    reason: Optional[str] = None
    window_days: Optional[int] = None
    problem_id: Optional[str] = None
    factor: Optional[str] = None
    lag_days: Optional[int] = None


@router.post("/vita/questions/resolve")
def vita_questions_resolve_endpoint(req: VitaQuestionResolveRequest, _: None = Depends(require_session)) -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                result = checks.resolve_question(
                    cur, req.question_id, req.source, req.action, req.title,
                    reason=req.reason, window_days=req.window_days, problem_id=req.problem_id,
                    factor=req.factor, lag_days=req.lag_days,
                )
            conn.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/vita/cases/{problem_id}/evidence")
def vita_case_evidence_endpoint(problem_id: str, _: None = Depends(require_session)) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            result = checks.case_evidence_view(cur, problem_id)
    if result is None:
        raise HTTPException(status_code=404, detail="кейс не найден")
    return result


# =====================================================================
# «Врач» и «Я» (макет v7) — вкладки были заглушками «скоро»
# =====================================================================

@router.get("/vita/doctor")
def vita_doctor_endpoint(lab: str = "", panel: int = 0,
                         _: None = Depends(require_session)) -> dict:
    # lab — выбрать лабу в карточке сдачи (тап по чипу), panel — вкладка дат
    # (живые dpill): обе опциональны, дефолт = cheapest-лаба и первая панель.
    with get_conn() as conn:
        with conn.cursor() as cur:
            return build_doctor(cur, lab=lab.strip() or None, panel=max(0, panel))


@router.get("/vita/medpassport")
def vita_medpassport_endpoint(_: None = Depends(require_session)) -> dict:
    from app import medpassport
    with get_conn() as conn:
        with conn.cursor() as cur:
            return medpassport.build_medpassport(cur)


@router.get("/vita/me")
def vita_me_endpoint(_: None = Depends(require_session)) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            return build_me(cur)


_TOPIC_SEGMENTS = ("recovery", "sleep", "move", "food")


@router.get("/vita/topic/{segment}")
def vita_topic_endpoint(segment: str, _: None = Depends(require_session)) -> dict:
    """Vita v2, этап 2 (живая просьба Влада) — детали конкретного кругляша:
    история/статистика из health.daily_trends + реальные публикации по теме,
    подгружается лениво по тапу (та же схема, что /vita/yesterday и
    /vita/cases/{id}/evidence — не раздувает /vita/today ради данных,
    нужных только при открытой шторке)."""
    if segment not in _TOPIC_SEGMENTS:
        raise HTTPException(status_code=404, detail=f"неизвестный сегмент: {segment}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            today = get_today_dashboard(cur)
            gate = build_gate(today)
            if segment == "recovery":
                return build_recovery_detail(cur, today, gate)
            if segment == "sleep":
                health = get_health_dashboard(cur)
                metric_by_key = {m["key"]: m for m in (health.get("metrics") or [])}
                sleep_min = metric_by_key.get("sleep_min", {}).get("value")
                sleep_quality = metric_by_key.get("sleep_score", {}).get("value")
                return build_sleep_detail(cur, sleep_min, sleep_quality)
            if segment == "move":
                steps = build_steps(cur, gate)
                return build_move_detail(cur, today, steps, gate)
            return build_food_topic_detail(cur)
