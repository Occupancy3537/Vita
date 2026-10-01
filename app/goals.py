# -*- coding: utf-8 -*-
"""«Мои цели» (2026-10-01, фаза 5 плана переделки): цели в одном месте, у каждой видно, кто её задал, и все можно менять,
кроме жёстких рамок врача.

Слои значения цели:
  1. базовое — константа кода (DEFAULT), профиль питания (health.nutrition_profile: ккал/белок/жиры/углеводы) или справочник
     норм (health.nutrient_targets: верхние пределы) — эти таблицы только на чтение (их периодически затирает синхронизация);
  2. переопределение Влада — card.goal.value (источник «я»);
  3. рамка врача — card.goal.frame_lo/frame_hi: выйти за неё нельзя, внутри рамки менять можно.

Потребители читают через get() (быстрый кэш на 30 с, сбрасывается при записи) или apply_* для строк из других таблиц.
Нет строки в card.goal или сбой чтения — действует базовое значение, ничего не ломается."""
import logging
import time
from dataclasses import dataclass
from typing import Optional

from psycopg import sql

from app.db import get_conn, schema

logger = logging.getLogger(__name__)

STEPS_TARGET_DAILY = 10000   # базовое значение цели по шагам (используется, если переопределения нет)
SOURCE_ME, SOURCE_DEFAULT = "я", "по умолчанию"
CACHE_TTL_SECONDS = 30


@dataclass(frozen=True)
class Goal:
    key: str
    title: str
    unit: str
    group: str
    default: float
    step: float
    lo: float                 # разумные границы ввода (не рамка врача)
    hi: float
    decimals: int = 0
    base: Optional[str] = None    # 'profile:<колонка>' | 'limit:<нутриент>' — откуда берётся базовое значение
    hint: Optional[str] = None


GOALS: list[Goal] = [
    Goal("steps_daily", "Шаги в день", "шагов", "Движение", STEPS_TARGET_DAILY, 500, 3000, 30000,
         hint="Консилиум 30 сент. рекомендовал 12–13 тыс. без резких скачков"),
    Goal("stand_gap_min", "Перерыв без движения не дольше", "мин", "Движение", 40, 5, 15, 120),
    Goal("stand_days_week", "Дней в неделе с таким перерывом", "дней", "Движение", 5, 1, 1, 7),
    Goal("swim_per_week", "Плавание", "раз в неделю", "Движение", 2, 1, 0, 7),
    Goal("sleep_min_h", "Сон не меньше", "ч", "Сон", 7, 0.5, 4, 9, decimals=1),
    Goal("sleep_max_h", "Сон не больше", "ч", "Сон", 9, 0.5, 7, 12, decimals=1),
    Goal("kcal", "Калории", "ккал", "Питание", 2400, 50, 1200, 4500, base="profile:Calories_target"),
    Goal("protein_g", "Белок", "г", "Питание", 160, 5, 40, 300, base="profile:Protein_target"),
    Goal("fat_g", "Жиры", "г", "Питание", 90, 5, 30, 200, base="profile:Fat_target"),
    Goal("carbs_g", "Углеводы", "г", "Питание", 270, 10, 50, 500, base="profile:Carbs_target"),
    Goal("sat_fat_g", "Насыщенные жиры, не больше", "г", "Лимиты", 27, 1, 5, 60, base="limit:Насыщенные жиры"),
    Goal("sodium_mg", "Натрий, не больше", "мг", "Лимиты", 2800, 100, 1000, 4000, base="limit:Натрий"),
    Goal("sugar_g", "Добавленный сахар, не больше", "г", "Лимиты", 50, 5, 10, 120, base="limit:Добавленный сахар"),
    Goal("caffeine_mg", "Кофеин, не больше", "мг", "Лимиты", 400, 50, 0, 800, base="limit:Кофеин"),
]
BY_KEY = {g.key: g for g in GOALS}
GROUPS = ["Движение", "Сон", "Питание", "Лимиты"]
_PROFILE_KEYS = {"kcal": "Calories_target", "protein_g": "Protein_target", "fat_g": "Fat_target", "carbs_g": "Carbs_target"}
_LIMIT_KEYS = {"sat_fat_g": "Насыщенные жиры", "sodium_mg": "Натрий", "sugar_g": "Добавленный сахар", "caffeine_mg": "Кофеин"}


class GoalError(ValueError):
    """Цель не принята: вне разумных границ или вне рамки врача (текст — для пользователя)."""


# ---------------------------------------------------------------- чтение (кэш)
_cache: dict = {"ts": 0.0, "rows": {}}


def cache_clear() -> None:
    _cache["ts"] = 0.0
    _cache["rows"] = {}


def _load_rows(cur) -> dict:
    cur.execute(sql.SQL("SELECT key, value, source, frame_lo, frame_hi, frame_source, frame_note, set_ts FROM {t}")
                .format(t=sql.Identifier(schema(), "goal")))
    out = {}
    for key, value, source, lo, hi, fsrc, fnote, ts in cur.fetchall():
        out[key] = {"value": float(value) if value is not None else None, "source": source,
                    "frame_lo": float(lo) if lo is not None else None, "frame_hi": float(hi) if hi is not None else None,
                    "frame_source": fsrc, "frame_note": fnote, "set_ts": ts.isoformat() if ts else None}
    return out


def rows(cur=None) -> dict:
    """Строки card.goal ({key: {...}}). Сбой чтения — пустой словарь (действуют базовые значения)."""
    now = time.monotonic()
    if cur is None and now - _cache["ts"] < CACHE_TTL_SECONDS:
        return _cache["rows"]
    try:
        if cur is None:
            with get_conn() as conn, conn.cursor() as c:
                data = _load_rows(c)
        else:
            data = _load_rows(cur)
    except Exception:
        logger.exception("goals: card.goal не прочитана — действуют базовые значения")
        data = {}
    if cur is None:
        _cache["ts"], _cache["rows"] = now, data
    return data


def override(key: str) -> Optional[float]:
    r = rows().get(key)
    return None if not r else r.get("value")


def get(key: str) -> float:
    """Значение цели для кода без собственной базовой таблицы (шаги, привычки, сон): переопределение или базовое."""
    ov = override(key)
    return ov if ov is not None else BY_KEY[key].default


def steps_target() -> int:
    return int(get("steps_daily"))


def sleep_zone_min() -> tuple[int, int]:
    """Зона сна в минутах (нижняя, верхняя)."""
    return round(get("sleep_min_h") * 60), round(get("sleep_max_h") * 60)


# ---------------------------------------------------------------- применение к строкам других таблиц
def apply_profile(profile: dict) -> dict:
    """Профиль питания с целями ккал/белок/жиры/углеводы, переопределёнными Владом (копия, исходный не меняется)."""
    out = dict(profile or {})
    for key, col in _PROFILE_KEYS.items():
        ov = override(key)
        if ov is not None:
            out[col] = str(int(ov)) if float(ov).is_integer() else str(ov)
    return out


def apply_limits(targets: list[dict]) -> list[dict]:
    """Строки nutrient_targets с верхними пределами, переопределёнными Владом (копия списка)."""
    ovs = {nutrient: override(key) for key, nutrient in _LIMIT_KEYS.items()}
    out = []
    for t in targets or []:
        v = ovs.get(t.get("Нутриент"))
        if v is not None:
            t = {**t, "Верхний_предел_UL": str(int(v)) if float(v).is_integer() else str(v)}
        out.append(t)
    return out


# ---------------------------------------------------------------- список для экрана и запись
def _base_values(cur) -> dict:
    """{key: (значение, подпись источника)} для целей с базой в других таблицах."""
    out = {}
    try:
        cur.execute('SELECT "Calories_target", "Protein_target", "Fat_target", "Carbs_target" FROM health.nutrition_profile '
                    'ORDER BY "User_ID" LIMIT 1')
        row = cur.fetchone()
        if row:
            for key, v in zip(("kcal", "protein_g", "fat_g", "carbs_g"), row):
                try:
                    out[key] = (float(str(v).replace(",", ".")), SOURCE_DEFAULT)
                except (TypeError, ValueError):
                    pass
        cur.execute('SELECT "Нутриент", "Верхний_предел_UL", "Источник" FROM health.nutrient_targets')
        by_n = {n: (ul, src) for n, ul, src in cur.fetchall()}
        for key, nutrient in _LIMIT_KEYS.items():
            ul, src = by_n.get(nutrient, (None, None))
            try:
                out[key] = (float(str(ul).replace(",", ".")), f"норма · {src}" if src else SOURCE_DEFAULT)
            except (TypeError, ValueError):
                pass
    except Exception:
        logger.exception("goals: базовые значения целей не прочитаны — берём значения по умолчанию")
    return out


def list_goals(cur) -> list[dict]:
    """Все цели с эффективным значением, источником и рамкой врача — для экрана «Мои цели»."""
    stored = rows(cur)
    base = _base_values(cur)
    out = []
    for g in GOALS:
        r = stored.get(g.key) or {}
        b_val, b_src = base.get(g.key, (g.default, SOURCE_DEFAULT))
        mine = r.get("value") is not None
        out.append({
            "key": g.key, "title": g.title, "unit": g.unit, "group": g.group, "step": g.step, "decimals": g.decimals,
            "lo": g.lo, "hi": g.hi, "hint": g.hint,
            "value": r["value"] if mine else b_val, "base": b_val,
            "source": SOURCE_ME if mine else b_src, "mine": mine,
            "frame": ({"lo": r.get("frame_lo"), "hi": r.get("frame_hi"), "source": r.get("frame_source"), "note": r.get("frame_note")}
                      if r.get("frame_lo") is not None or r.get("frame_hi") is not None else None),
        })
    return out


def _check(g: Goal, value: float, frame: Optional[dict]) -> float:
    if not (g.lo <= value <= g.hi):
        raise GoalError(f"«{g.title}»: допустимо от {g.lo:g} до {g.hi:g} {g.unit}")
    if frame:
        lo, hi = frame.get("frame_lo"), frame.get("frame_hi")
        why = frame.get("frame_source") or "врач"
        if hi is not None and value > hi:
            raise GoalError(f"Рамка врача ({why}): не больше {hi:g} {g.unit}")
        if lo is not None and value < lo:
            raise GoalError(f"Рамка врача ({why}): не меньше {lo:g} {g.unit}")
    return round(value, g.decimals)


def set_goal(cur, key: str, value: float) -> dict:
    g = BY_KEY.get(key)
    if g is None:
        raise GoalError("Неизвестная цель")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise GoalError("Нужно число")
    existing = rows(cur).get(key)
    value = _check(g, value, existing)
    cur.execute(
        sql.SQL("INSERT INTO {t} (key, value, source, prev_value, set_ts) VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET prev_value = {t}.value, value = EXCLUDED.value, source = EXCLUDED.source, set_ts = now()")
        .format(t=sql.Identifier(schema(), "goal")),
        (key, value, SOURCE_ME, (existing or {}).get("value")))
    cache_clear()
    return {"key": key, "value": value}


def reset_goal(cur, key: str) -> None:
    """Сбросить переопределение (рамка врача остаётся)."""
    if key not in BY_KEY:
        raise GoalError("Неизвестная цель")
    cur.execute(sql.SQL("UPDATE {t} SET value = NULL, source = %s, set_ts = now() WHERE key = %s")
                .format(t=sql.Identifier(schema(), "goal")), (SOURCE_DEFAULT, key))
    cache_clear()
