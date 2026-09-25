"""
Ворота рождения рекомендации (П3 §2.2, G1-G6) — Gap 2 из CARD_ARCHITECTURE_PLAN
§5. Спека дословно: «у советника нет привилегированного пути записи» — черновик
проходит все шесть ворот ДЕТЕРМИНИРОВАННО, ДО того как стать rc_.

Честно об объёме: G3 (интеракции) и G4 (gate-совместимость) реализованы как
keyword-совпадение — тот же MVP-уровень, что уже был у check_bracelet_intersection
в write_path.py, ДО полноценного entity_index (П4, отдельная фаза). Это значит:
ловит совпадения по словам/синонимам из словаря ниже, не понимает произвольную
перефразировку. Лучше, чем ничего, не замена настоящему retrieval.
"""
import re
from dataclasses import dataclass
from typing import Optional

from psycopg import sql

from app.db import schema
from app.write_path import check_bracelet_intersection


@dataclass
class GateFailure:
    gate: str
    reason: str
    ref_id: Optional[str] = None  # для G5 duplicate/conflict — id существующей rc_, на которую ссылаемся


# G1 — физиологическая правдоподобность величины ожидания. Метрики вне списка не
# блокируются по magnitude (нет данных, чтобы судить) — окно/lag всё равно проверяются.
PHYSIOLOGICAL_MAX_DELTA = {
    "hrv": 30.0, "rhr": 15.0, "sleep_min": 90.0, "sleep_score": 30.0,
    "steps": 8000.0, "stress": 30.0, "body_battery": 40.0, "vo2max": 5.0, "weight": 5.0,
}


def gate1_sanity(metric_key: Optional[str], direction: Optional[str], magnitude: Optional[float],
                  window_days: int, lag_days: int) -> Optional[GateFailure]:
    if not (7 <= window_days <= 180):
        return GateFailure("G1", f"window_days={window_days} вне диапазона 7-180")
    if lag_days > window_days / 2:
        return GateFailure("G1", f"lag_days={lag_days} > window_days/2={window_days / 2}")
    if metric_key and magnitude is not None:
        max_delta = PHYSIOLOGICAL_MAX_DELTA.get(metric_key)
        if max_delta is not None and abs(magnitude) > max_delta:
            return GateFailure(
                "G1", f"magnitude={magnitude} превышает физиологический предел ±{max_delta} для {metric_key}")
    return None


def gate2_measurability(cur, metric_key: Optional[str]) -> str:
    """Возвращает режим, не блокирует никогда (спека: провал G2 = деградация, не reject)."""
    if metric_key is None:
        return "subjective"
    cur.execute(
        sql.SQL("SELECT 1 FROM {table} WHERE metric_key = %s").format(table=sql.Identifier(schema(), "metric_coverage")),
        (metric_key,),
    )
    return "measurable" if cur.fetchone() is not None else "unmeasurable"


# G4 — синонимы к формулировкам gate.contra_* (см. card.problem, реальный гейт Влада
# по L5/S1: "статические удержания", "осевая нагрузка", "бег, прыжки, интервалы" и
# т.п.). Ключ — фрагмент, который реально встречается в gate-полях; значения —
# слова, которыми это может назвать советник в рекомендации. Список растёт по факту
# столкновений, как словарь красных флагов — не претендует на полноту без П4.
CONTRA_SYNONYMS = {
    "статические удержания": ["изометри", "планк", "статичес", "удержан"],
    "осевая нагрузка": ["осев", "штанг", "присед", "становая", "жим ", "гантел"],
    "скручивания": ["скручив", "пресс", "кранч"],
    "бег, прыжки, интервалы": ["бег", "прыжк", "интервальн", "спринт", "кроссфит"],
    "длительное сидение без опоры": ["сидение без опоры", "долго сидеть"],
    "подъём >2 кг": ["подъём тяж", "поднимать тяж", "тяжест"],
}


def _contra_phrases(gate: dict) -> list[str]:
    raw = " ; ".join(filter(None, [gate.get("contra_load"), gate.get("contra_other"), gate.get("contra_food")]))
    return [p.strip().lower() for p in re.split(r"[;]", raw) if p.strip()]


def gate3_interaction(text: str) -> Optional[GateFailure]:
    lowered = text.lower()
    bracelet_hits = check_bracelet_intersection(lowered)
    if bracelet_hits:
        return GateFailure("G3", f"contraindicated: пересечение с браслетом ({', '.join(bracelet_hits)})")
    return None


def gate4_gate_compat(cur, text: str) -> Optional[GateFailure]:
    lowered = text.lower()
    cur.execute(
        sql.SQL("SELECT title, gate FROM {table} WHERE status = 'active' AND gate IS NOT NULL")
        .format(table=sql.Identifier(schema(), "problem")),
    )
    for title, gate in cur.fetchall():
        for phrase in _contra_phrases(gate):
            keywords = CONTRA_SYNONYMS.get(phrase, [phrase])
            for kw in keywords:
                if kw and kw in lowered:
                    return GateFailure("G4", f"«{kw}» противоречит ограничению «{title}» (gate: {phrase})")
    return None


def gate5_dedup(cur, kind: Optional[str], action: Optional[str],
                 metric_key: Optional[str], direction: Optional[str]) -> Optional[GateFailure]:
    rec_table = sql.Identifier(schema(), "recommendation")
    if kind and action:
        cur.execute(
            sql.SQL("SELECT id FROM {table} WHERE status = 'active' AND kind = %s AND action = %s")
            .format(table=rec_table),
            (kind, action),
        )
        row = cur.fetchone()
        if row:
            return GateFailure("G5", f"уже есть активная рекомендация с тем же kind+action: {row[0]}", ref_id=row[0])

    if metric_key and direction:
        cur.execute(
            sql.SQL(
                "SELECT r.id, ex.direction FROM {ex} ex JOIN {rc} r ON r.id = ex.rec_id "
                "WHERE r.status = 'active' AND ex.role = 'primary' AND ex.metric_key = %s"
            ).format(ex=sql.Identifier(schema(), "expectation"), rc=rec_table),
            (metric_key,),
        )
        for rec_id, existing_direction in cur.fetchall():
            if existing_direction != direction:
                return GateFailure(
                    "G5",
                    f"конфликт направления по {metric_key}: активная {rec_id} уже просит '{existing_direction}', "
                    f"новая — '{direction}' — нужно serial-испытание (не автоматизировано, ручное решение)",
                    ref_id=rec_id,
                )
    return None


def gate7_expectation_required(metric_key: Optional[str], direction: Optional[str],
                                magnitude: Optional[float], unmeasurable_reason: Optional[str]) -> Optional[GateFailure]:
    """G7 (петля исходов, 2026-09-24) — рекомендация рождается ЛИБО с проверяемым
    ожиданием (metric_key+direction+magnitude — движок посчитает вердикт), ЛИБО с
    явной причиной, почему это невозможно (unmeasurable_reason). Раньше "неизмеримо"
    означало ноль строк expectation вообще — неотличимо от "советник забыл дать
    ожидание". Теперь оба случая дают ex_-запись (см. sync_recommendation), но G7
    гарантирует, что причина для отказа от метрики была НАЗВАНА, а не подразумевалась."""
    has_expectation = metric_key is not None and direction is not None and magnitude is not None
    has_reason = bool(unmeasurable_reason and unmeasurable_reason.strip())
    if not has_expectation and not has_reason:
        return GateFailure(
            "G7",
            "нет ни проверяемого ожидания (metric_key+direction+magnitude), ни explicit "
            "unmeasurable_reason — совет должен либо порождать проверку, либо явно "
            "признавать, что не измерим (и почему)",
        )
    return None


def gate6_priority(is_bioage_driver: bool, metric_overdue: bool) -> str:
    """Метрика — тормоз-драйвер биовозраста ИЛИ просроченная лаба -> high.

    2026-09-22 (F10 аудита логики): прежняя редакция докстринга утверждала, что
    «card-service не имеет доступа к схеме health» — устарело с Phase 0 нового
    доктора (см. app/db.py: роли выдан SELECT на 12 таблиц health и запись в 4).
    Флаги всё равно считает вызывающий — но уже как осознанный выбор, а не
    «физическое ограничение»: советник получает is_bioage_driver/metric_overdue
    из собственного анализа (у него эти данные уже под рукой), а ворота остаются
    чистыми функциями без кросс-схемных запросов."""
    return "high" if (is_bioage_driver or metric_overdue) else "normal"
