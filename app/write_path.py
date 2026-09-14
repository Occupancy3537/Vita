"""
Write-path (П2 §3): красный флаг слой A -> LLM-извлечение -> детерминированная
валидация (дедуп эпизодов, браслетное пересечение) -> запись объектов -> journal.

Полный конвейер описан в CARD_ARCHITECTURE_PLAN_2026-09-13.md. Здесь — MVP объём
Phase 2: дедуп эпизодов по таблице решений §3.3, браслетное пересечение простым
ключевым совпадением (полноценный entity_index/словарь — П4, отдельная фаза).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from ulid import ULID

from app import redflag
from app.db import get_conn, schema
from app.extraction import Draft, extract, PROMPT_VERSION
from app.journal import write_journal

# Простые ключевые слова для браслетного пересечения — MVP пока нет entity_index (П4).
# Ключ metric_key браслетного факта -> русские слова, по которым его можно узнать в тексте.
BRACELET_KEYWORDS = {
    "allergy:novocaine_anaphylaxis": ["новокаин"],
    "allergy:penicillin": ["пенициллин"],
    "allergy:phosphates_sulfates": ["фосфат", "сульфат"],
    "allergy:pollock_fish": ["минтай"],
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def prov(origin: str, extra: Optional[dict] = None) -> str:
    base = {"origin": origin, "source_id": None, "extraction": None, "model": None, "prompt_version": None}
    if extra:
        base.update(extra)
    return json.dumps(base)


def check_bracelet_intersection(text: str) -> list[str]:
    """F7-подобная проверка (П5, упрощённо для Phase 2): есть ли в тексте упоминание
    сущности из браслета. Возвращает список задетых bracelet-ключей."""
    lowered = text.lower()
    hits = []
    for key, words in BRACELET_KEYWORDS.items():
        if any(w in lowered for w in words):
            hits.append(key)
    return hits


def find_open_episode(cur, symptom_key: str, within_hours: float = 48.0):
    cur.execute(
        f"SELECT id, ts_event FROM {schema()}.episode "
        f"WHERE symptom_key = %s AND status = 'open' ORDER BY ts_event DESC LIMIT 1",
        (symptom_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    ep_id, ts_event = row
    if datetime.now(timezone.utc) - ts_event <= timedelta(hours=within_hours):
        return ep_id
    return None


def apply_draft(cur, draft: Draft, source_id: str) -> dict:
    """Таблица решений П2 §3.3 (обновить/создать/закрыть эпизод)."""
    open_ep = find_open_episode(cur, draft.symptom_key)

    if draft.negation and open_ep:
        cur.execute(
            f"UPDATE {schema()}.episode SET status = 'resolved', end_ts = now(), closure_source = 'user' "
            f"WHERE id = %s",
            (open_ep,),
        )
        write_journal(cur, "episode", open_ep, "close",
                      diff={"status": "resolved", "closure_source": "user"}, reason=f"source={source_id}")
        return {"action": "closed_episode", "episode_id": open_ep}

    if open_ep and not draft.negation:
        fact_id = new_id("f")
        attrs = {"intensity": draft.intensity, "triggers": draft.triggers}
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_text, episode_id, attrs) "
            f"VALUES (%s, now(), %s, 'auto', %s, %s, %s, %s)",
            (fact_id, prov("llm_extract", {"source_id": source_id}), "symptom:" + draft.symptom_key,
             draft.onset_expr or "", open_ep, json.dumps(attrs)),
        )
        write_journal(cur, "fact", fact_id, "create",
                      diff={"metric_key": "symptom:" + draft.symptom_key, "episode_id": open_ep,
                            "value_text": draft.onset_expr or "", "attrs": attrs},
                      reason=f"source={source_id}", link_back=True)
        return {"action": "updated_episode", "episode_id": open_ep, "fact_id": fact_id}

    # Новый эпизод.
    ep_id = new_id("ep")
    cur.execute(
        f"INSERT INTO {schema()}.episode (id, ts_event, provenance, verification, symptom_key, onset_ts, status, intensity, triggers, context) "
        f"VALUES (%s, now(), %s, 'auto', %s, now(), 'open', %s, %s, %s)",
        (ep_id, prov("llm_extract", {"source_id": source_id}), draft.symptom_key,
         draft.intensity, draft.triggers, draft.onset_expr),
    )
    write_journal(cur, "episode", ep_id, "create",
                  diff={"symptom_key": draft.symptom_key, "status": "open", "intensity": draft.intensity,
                        "triggers": draft.triggers, "context": draft.onset_expr},
                  reason=f"source={source_id}", link_back=True)
    fact_id = new_id("f")
    attrs = {"intensity": draft.intensity, "triggers": draft.triggers}
    cur.execute(
        f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_text, episode_id, attrs) "
        f"VALUES (%s, now(), %s, 'auto', %s, %s, %s, %s)",
        (fact_id, prov("llm_extract", {"source_id": source_id}), "symptom:" + draft.symptom_key,
         draft.onset_expr or "", ep_id, json.dumps(attrs)),
    )
    write_journal(cur, "fact", fact_id, "create",
                  diff={"metric_key": "symptom:" + draft.symptom_key, "episode_id": ep_id,
                        "value_text": draft.onset_expr or "", "attrs": attrs},
                  reason=f"source={source_id}", link_back=True)
    return {"action": "created_episode", "episode_id": ep_id, "fact_id": fact_id}


def process(source_id: str) -> dict:
    """Контракт process(src_id) -> {written, questions, flags} (П1 §5)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT raw_text FROM {schema()}.source_message WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"source_message {source_id} не найден")
            raw_text = row[0]

            # 1. Слой A — ДО и НЕЗАВИСИМО от извлечения.
            rf = redflag.detect(raw_text)
            soft = redflag.soft_detect(raw_text) if not rf["hit"] else []

            # 2. Браслетное пересечение — тоже до извлечения, по сырому тексту.
            bracelet_hits = check_bracelet_intersection(raw_text)

            written = []
            questions = []

            # 3. LLM-извлечение (не участвует в red_flag/bracelet решении выше).
            result = extract(raw_text)

            if not result.no_medical_content:
                for draft in result.drafts:
                    outcome = apply_draft(cur, draft, source_id)
                    written.append(outcome)

            # 3b. Извлечение — трассируемость "что модель вернула" отдельно от того,
            # что реально попало в карту после валидации (Gap 1, П2 требует этого
            # шага явно — раньше терялось между red-flag'ами и записью объектов).
            extraction_id = new_id("x")
            extraction_status = (
                "no_medical" if result.no_medical_content
                else "applied" if written
                else "empty"
            )
            cur.execute(
                f"INSERT INTO {schema()}.extraction (id, source_id, model, prompt_version, drafts_json, flags_json, status) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (extraction_id, source_id, result.model, PROMPT_VERSION,
                 json.dumps([d.model_dump() for d in result.drafts]),
                 json.dumps({"no_medical_content": result.no_medical_content, "bracelet_hits": bracelet_hits}),
                 extraction_status),
            )

            cur.execute(
                f"UPDATE {schema()}.source_message SET status = 'processed', processed_at = now() WHERE id = %s",
                (source_id,),
            )
        conn.commit()

    if rf["hit"]:
        questions.append(f"КРАСНЫЙ ФЛАГ ({rf['category']}): {', '.join(rf['matched'])}")
    if bracelet_hits:
        questions.append(f"Пересечение с браслетом: {', '.join(bracelet_hits)}")
    if soft:
        questions.extend(soft)

    return {"written": written, "questions": questions, "flags": {"red_flag": rf, "bracelet_hits": bracelet_hits}}
