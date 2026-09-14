"""
П5 §1-§6 — союз детекторов красных флагов: A (regex, мгновенно) + B (LLM,
контекстно-свободно) + bracelet-cross (F7) + C (метрические правила). Уровень —
детерминированная таблица §2.3 (F5, "уровень — функция, не мнение"). Сессии
против спама на повторные упоминания одного эпизода (F8).

Честно об объёме: реализована таблица §2.3 для основных веток (critical/high/
systemic_warning), rf-c-03 (cross B+C) — нет (нужен реальный B-флаг того же дня,
добавляется по факту первого столкновения, тот же принцип, что и остальные
словари проекта). Сессии — упрощённая версия (открыта/закрыта по 48ч тишине или
явному урегулированию), без полного набора переходов status (acknowledged и
т.п. — управляются вручную через API, не автоматической машиной состояний).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg import sql
from ulid import ULID

from app import redflag
from app.db import schema
from app.journal import write_journal
from app.redflag_b import LayerBResult
from app.write_path import check_bracelet_intersection

_LEVEL_ORDER = {"L1": 1, "L2": 2, "L3": 3}
SESSION_TTL_HOURS = 48


def level_for(category: str, modality: dict, severity_factors: Optional[dict] = None) -> Optional[str]:
    """§2.3 — детерминированная таблица уровней. Возвращает L1/L2/L3 или None."""
    if category == "systemic_warning":
        return "L1"  # C (и слабые B-сигналы этой категории) никогда не выше L1

    critical = category in redflag.CRITICAL_CATEGORIES
    high = category in redflag.HIGH_CATEGORIES
    current = modality.get("current", True)
    past = modality.get("past", False)
    negation = modality.get("negation", False)
    hypothetical = modality.get("hypothetical", False)
    third_party = modality.get("third_party", False)

    if critical:
        if current and not (past or negation or hypothetical or third_party):
            return "L3"
        return "L1"  # past/negation/hypothetical/third_party — все ветки критической таблицы дают L1

    if high:
        if not current:
            return None
        has_factors = bool(severity_factors and (severity_factors.get("duration_min") or severity_factors.get("combination")))
        return "L3" if has_factors else "L2"

    return None


def evaluate_union(bracelet_hits: list[str], layer_a_hits: list[dict],
                    layer_b: Optional[LayerBResult] = None, layer_c: Optional[list[dict]] = None) -> dict:
    """Худший (наивысший) уровень среди всех сработавших источников — независимость
    союза (F9): ни один слой не может "отменить" флаг другого."""
    candidates = []

    if bracelet_hits:
        # F7: пересечение с браслетом = немедленная L3, канонический сквозной тест союза.
        candidates.append({"level": "L3", "category": "anaphylaxis", "source": "bracelet_cross",
                            "rule_ref": ",".join(bracelet_hits), "confidence": 1.0,
                            "context_note": "упоминание сущности из браслета: " + ", ".join(bracelet_hits)})

    for hit in layer_a_hits:
        # F6: A эскалирует мгновенно, до контекста/модальности — узкие regex-паттерны
        # уже сконструированы так, чтобы не ловить прошлое/гипотетическое.
        candidates.append({"level": "L3", "category": hit["category"], "source": "A",
                            "rule_ref": hit["label"], "confidence": 1.0, "context_note": hit["label"]})

    if layer_b and layer_b.hit and not layer_b.degraded:
        lvl = level_for(layer_b.category, layer_b.modality.model_dump(), layer_b.severity_factors.model_dump())
        if lvl:
            candidates.append({"level": lvl, "category": layer_b.category, "source": "B",
                                "rule_ref": None, "confidence": layer_b.confidence,
                                "context_note": layer_b.context_note})

    for c in (layer_c or []):
        candidates.append({"level": c["level"], "category": c["category"], "source": "C",
                            "rule_ref": c["rule"], "confidence": None, "context_note": c["message"]})

    if not candidates:
        return {"level": None, "sources": []}

    best = max(candidates, key=lambda c: _LEVEL_ORDER[c["level"]])
    return {**best, "sources": candidates}


def _find_open_session(cur, category: str):
    cur.execute(
        sql.SQL(
            "SELECT id, worst_level FROM {t} WHERE category = %s AND status = 'open' "
            "AND last_activity >= now() - (%s || ' hours')::interval ORDER BY last_activity DESC LIMIT 1"
        ).format(t=sql.Identifier(schema(), "rf_session")),
        (category, SESSION_TTL_HOURS),
    )
    return cur.fetchone()


def record_rf_event(cur, result: dict, source_id: Optional[str] = None,
                     ts_event: Optional[datetime] = None) -> Optional[dict]:
    """F8: повторные упоминания одного эпизода дописываются в открытую сессию, не
    порождают новую эскалацию каждый раз. Уровень сессии не понижается."""
    if not result.get("level"):
        return None
    ts_event = ts_event or datetime.now(timezone.utc)
    category = result["category"]

    session_row = _find_open_session(cur, category)
    if session_row:
        session_id, worst_level = session_row
        new_worst = worst_level if _LEVEL_ORDER[worst_level] >= _LEVEL_ORDER[result["level"]] else result["level"]
        cur.execute(
            sql.SQL("UPDATE {t} SET last_activity = now(), worst_level = %s WHERE id = %s")
            .format(t=sql.Identifier(schema(), "rf_session")),
            (new_worst, session_id),
        )
    else:
        session_id = f"rfs_{ULID()}"
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, category, worst_level) VALUES (%s, %s, %s)")
            .format(t=sql.Identifier(schema(), "rf_session")),
            (session_id, category, result["level"]),
        )

    event_id = f"rf_{ULID()}"
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, ts_event, provenance, category, level, source, rule_ref, "
            "source_message_ids, session_id, context_note, confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        ).format(t=sql.Identifier(schema(), "rf_event")),
        (event_id, ts_event, json.dumps({"origin": "redflag_union"}), category, result["level"],
         result["source"], result.get("rule_ref"), json.dumps([source_id] if source_id else []),
         session_id, result.get("context_note"), result.get("confidence")),
    )
    write_journal(cur, "rf_event", event_id, "create",
                  diff={"category": category, "level": result["level"], "source": result["source"],
                        "session_id": session_id})

    return {"event_id": event_id, "session_id": session_id, "level": result["level"], "category": category}


def evaluate_and_record(cur, text: str, source_id: Optional[str] = None,
                         layer_b: Optional[LayerBResult] = None) -> dict:
    """Полный проход: A + bracelet-cross (детерминированно, из сырого текста) +
    опциональный уже посчитанный B (§1.1 — B параллелен extraction, вызывается
    отдельно вызывающим, не здесь — не задерживаем детерминированную часть
    LLM-вызовом). Layer C — фактовый, не текстовый, вызывается отдельно
    (app.redflag_c.run_layer_c) и передаётся сюда при желании объединить в одну
    запись; в этой функции не запускается сам по себе."""
    bracelet_hits = check_bracelet_intersection(text)
    layer_a_hits = redflag.detect_categorized(text)
    result = evaluate_union(bracelet_hits, layer_a_hits, layer_b)
    recorded = record_rf_event(cur, result, source_id) if result.get("level") else None
    return {"result": result, "recorded": recorded}
