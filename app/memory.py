"""
П4 — Память: слои (браслет/горячий/холодный), retrieval, get_context(). Gap 3 из
CARD_ARCHITECTURE_PLAN_2026-09-13.md §5.

Ключевая идея пакета (дословно из спеки): память советника НЕ хранилище — три
детерминированных представления над картой плюс конвейер доставки нужного под
конкретный вопрос. Ничего не копируется; «в голове» только рендеры.

Честно об объёме этого прохода (не претендует на 100% спеки за один заход):
- Реализовано: слои (браслет/горячее с бюджетом и деградацией), entity_index +
  retrieval (L1 словарь, L2 LLM-fallback ТОЛЬКО когда L1 пуст — каждый LLM-вызов
  это +10-15 сек по замеру сегодняшней сессии, не вызываем его без нужды), tier-1
  рендер с агрегацией, rehydration (get_object), get_context() по контракту §5
  (включая обязательный missing[]), access-метрики, memory_note (create для
  clinical-типа — из вердиктов П3), предархивная проверка забывания (§6.2).
- НЕ реализовано в этом заходе (честно, не скрыто): case_summary при закрытии
  проблемы (§6.3) — в системе физически нет пути ЗАКРЫТЬ проблему (единственная
  card.problem — это миграционная L5/S1 запись, create/close для problem не
  существует вообще, это отдельная работа, не входящая в П4 узко); session_digest
  по теме разговора (§7, триггер "≥20 реплик по теме") — у source_message нет
  привязки к теме/треду, только 24ч-тишина как триггер технически проверяема без
  этого поля, что и сделано отдельной функцией; watchdog-триггеры (§9) описаны как
  чистые функции здесь, но не подключены к расписанию — это интеграционный шаг в
  n8n, не код card-service; golden-корпус retrieval (§14, ≥100 пар) — начат в
  tests/test_memory.py на реальных сущностях Влада, не 100+ (тот же принцип, что
  красные флаги: полный корпус — с его участием, не в одиночку).
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from psycopg import sql

from app.db import schema
from app.extraction import MODEL, OPENROUTER_URL, PROVIDER_ORDER
import os

RENDERER_VERSION = "memory-render/1"
HOT_BUDGET_TOKENS = 1200
TIER1_BUDGET_TOKENS = 2000
TIER1_MAX_OBJECTS = 12
AGGREGATE_THRESHOLD = 5  # >K объектов одного типа/сущности -> агрегат, не перечисление

# --- словарь канонических сущностей (L1) -----------------------------------
# Стартовый набор на реальных данных Влада (браслет + уже виденные symptom_key) —
# растёт по столкновениям, тот же принцип, что словарь красных флагов/G4-синонимов.
CANONICAL_ENTITIES: dict[str, tuple[str, str]] = {
    # фраза -> (entity_type, canonical_value)
    "новокаин": ("substance", "novocaine"),
    "лидокаин": ("substance", "lidocaine"),
    "пенициллин": ("substance", "penicillin"),
    "минтай": ("substance", "pollock"),
    "фосфат": ("substance", "phosphates"),
    "сульфат": ("substance", "sulfates"),
    "голов": ("symptom", "headache"),  # голова/головой/головы/голове — общий стем
    "мигрен": ("symptom", "headache"),
    "спин": ("symptom", "back_pain"),  # спина/спину/спине/спиной
    "поясниц": ("symptom", "back_pain"),
    "грыж": ("problem", "l5_s1_hernia"),
    "l5": ("problem", "l5_s1_hernia"),
    "локот": ("symptom", "elbow_pain"),  # ловит "локоть" (не ловит "локтя" — беглая
    # гласная меняет позицию при склонении, известное ограничение словаря без П4-retrieval)
    "изжог": ("symptom", "heartburn"),
    "витамин д": ("substance", "vitamin_d"),  # кириллическая "д", не латинская "d"
    "магни": ("substance", "magnesium"),
    "ягодиц": ("symptom", "hip_pain"),
    "колен": ("symptom", "knee_pain"),
    "горло": ("symptom", "throat_irritation"),
    "перш": ("symptom", "throat_irritation"),
    "кист": ("symptom", "wrist_pain"),
    "запясть": ("symptom", "wrist_pain"),
    "герпес": ("symptom", "herpes_rash"),
    "подреберь": ("symptom", "hypochondrium_pain"),
}

# Историческая миграция (Phase 1) записала symptom_key как транслитерированные
# русские слаги старой системы Symptom_Log ("golovnaya-bol"), а не английские
# ключи нового пайплайна извлечения ("headache") — без этой карты retrieval не
# нашёл бы всю историю Влада ДО подключения card-service, только события после.
# Разово используется бэкфиллом entity_index (backups/infra/backfill_entity_index.py).
LEGACY_SYMPTOM_ALIAS = {
    "bol-kist-posle-broskov": "wrist_pain",
    "bol-levoe-podrebere-posle-edy": "hypochondrium_pain",
    "bol-lokot-posle-broskov": "elbow_pain",
    "bol-pod-levym-kolenom": "knee_pain",
    "bol-poyasnica-radikulopatiya": "back_pain",
    "bol-pravaya-yagodica": "hip_pain",
    "bol-pravoe-podrebere-posle-zhirnogo": "hypochondrium_pain",
    "gerpes-vysypanie": "herpes_rash",
    "golovnaya-bol": "headache",
    "izzhoga": "heartburn",
    "pershenie-v-gorle": "throat_irritation",
    "sliz-v-nosoglotke": "nasal_mucus",
    "suhost-kozhi-palcev": "dry_skin_fingers",
    "zabolelo-serdce-nejromidin": "heart_pain_neuromidin",
    "slabost-pomutnenie-produkt": "weakness_food_related",
}


def _num_tokens(text: str) -> int:
    """Грубая оценка (символы/4 — стандартная эвристика), достаточно для бюджетов
    версионируемых как параметр (memory-config/1 в терминах спеки), не претендует
    на точный токенайзер конкретной модели."""
    return max(1, len(text) // 4)


def _render_version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


# --- 1. Браслет (§1.1) ------------------------------------------------------

def render_bracelet(cur) -> str:
    lines = []
    cur.execute(
        sql.SQL("SELECT metric_key, value_text, verification FROM {t} WHERE salience = 'bracelet' ORDER BY ts_event")
        .format(t=sql.Identifier(schema(), "fact")),
    )
    for metric_key, value_text, verification in cur.fetchall():
        lines.append(f"• {metric_key}: {value_text} ({verification})")

    cur.execute(
        sql.SQL("SELECT title, gate, verification FROM {t} WHERE salience = 'bracelet' ORDER BY ts_event")
        .format(t=sql.Identifier(schema(), "problem")),
    )
    for title, gate, verification in cur.fetchall():
        gate_txt = ""
        if gate:
            allowed = gate.get("allowed")
            gate_txt = f" — gate: разрешено «{allowed}»" if allowed else ""
        lines.append(f"• диагноз: {title}{gate_txt} ({verification})")

    cur.execute(
        sql.SQL("SELECT name, dose FROM {t} WHERE salience = 'bracelet' ORDER BY ts_event")
        .format(t=sql.Identifier(schema(), "intervention")),
    )
    for name, dose in cur.fetchall():
        lines.append(f"• adverse/значимо: {name}" + (f" ({dose})" if dose else ""))

    body = "\n".join(lines) if lines else "(браслет пуст)"
    return f"[БРАСЛЕТ · {_render_version(body)}]\n{body}"


# --- 2. Горячий слой (§1.2) с бюджетом и деградацией ------------------------

def _hot_problems(cur) -> list[str]:
    cur.execute(
        sql.SQL("SELECT title, status FROM {t} WHERE status IN ('active','monitoring') ORDER BY ts_event DESC")
        .format(t=sql.Identifier(schema(), "problem")),
    )
    return [f"«{title}» {status}" for title, status in cur.fetchall()]


def _hot_recommendations(cur) -> list[str]:
    cur.execute(
        sql.SQL(
            "SELECT r.title, r.status, r.cycle, ex.metric_key, ex.direction, ex.magnitude "
            "FROM {rc} r LEFT JOIN {ex} ex ON ex.rec_id = r.id AND ex.role = 'primary' "
            "WHERE r.status IN ('active','accepted','evaluating','paused_adverse') ORDER BY r.ts_event DESC"
        ).format(rc=sql.Identifier(schema(), "recommendation"), ex=sql.Identifier(schema(), "expectation")),
    )
    out = []
    for title, status, cycle, metric_key, direction, magnitude in cur.fetchall():
        expect = f"; ожидание {metric_key} {direction} {magnitude}" if metric_key else ""
        out.append(f"«{title}» цикл {cycle}, статус {status}{expect}")
    return out


def _hot_episodes(cur) -> list[str]:
    cur.execute(
        sql.SQL(
            "SELECT symptom_key, status, ts_event, end_ts, intensity FROM {t} "
            "WHERE (status = 'open' AND ts_event >= now() - interval '30 days') "
            "OR (status != 'open' AND end_ts >= now() - interval '14 days') "
            "ORDER BY ts_event DESC"
        ).format(t=sql.Identifier(schema(), "episode")),
    )
    out = []
    for symptom_key, status, ts_event, end_ts, intensity in cur.fetchall():
        marker = "открыт" if status == "open" else f"закрыт {end_ts.date() if end_ts else '?'}"
        out.append(f"{symptom_key} ({marker}, с {ts_event.date()}" + (f", интенсивность {intensity}" if intensity else "") + ")")
    return out


def _hot_open_questions(cur) -> list[str]:
    cur.execute(
        sql.SQL("SELECT class, opinion_advisor FROM {t} WHERE status = 'raised' ORDER BY ts_event DESC")
        .format(t=sql.Identifier(schema(), "disagreement")),
    )
    return [f"расхождение ({cls}): {op or '?'}" for cls, op in cur.fetchall()]


def _hot_last_visit_and_labs(cur) -> list[str]:
    out = []
    cur.execute(
        sql.SQL("SELECT title, ts_event FROM {t} ORDER BY ts_event DESC LIMIT 1")
        .format(t=sql.Identifier(schema(), "visit")),
    )
    row = cur.fetchone()
    if row:
        out.append(f"последний визит: {row[0] or '(без заголовка)'} ({row[1].date()})")

    cur.execute(
        sql.SQL(
            "SELECT marker_label, value_num, unit, ref_min, ref_max FROM {t} "
            "WHERE value_num IS NOT NULL AND ref_min IS NOT NULL AND ref_max IS NOT NULL "
            "AND (value_num < ref_min OR value_num > ref_max) "
            "ORDER BY ts_event DESC LIMIT 5"
        ).format(t=sql.Identifier(schema(), "lab_result")),
    )
    outliers = [f"{label or '?'} {num}{unit or ''} (реф. {rmin}-{rmax})" for label, num, unit, rmin, rmax in cur.fetchall()]
    if outliers:
        out.append("лабы вне референса: " + "; ".join(outliers))
    return out


def _hot_notes(cur) -> list[str]:
    """layer='hot' — вычисляемое поле рендера (§2), не хранимое: pinned ИЛИ создано
    в последние 60 дней (§2.3 "субъект закрыт 60д -> cold" — упрощаем до возраста
    заметки, т.к. полного отслеживания "закрытия субъекта" для произвольной mn_
    пока нет)."""
    cur.execute(
        sql.SQL(
            "SELECT title, type FROM {t} WHERE superseded_by IS NULL "
            "AND (salience_locked OR valid_from >= now() - interval '60 days') "
            "ORDER BY valid_from DESC"
        ).format(t=sql.Identifier(schema(), "memory_note")),
    )
    return [f"[{title or type_}]" for title, type_ in cur.fetchall()]


def _track_record_digest(cur) -> Optional[str]:
    cur.execute(
        sql.SQL("SELECT verdict, count(*) FROM {t} WHERE status = 'current' GROUP BY verdict")
        .format(t=sql.Identifier(schema(), "recommendation_verdict")),
    )
    counts = dict(cur.fetchall())
    total = sum(counts.values())
    if not total:
        return None
    parts = [f"{v} {counts[v]}" for v in ("effective", "partial", "no_effect", "adverse", "data_gap") if counts.get(v)]
    return f"{total} испытаний — " + ", ".join(parts)


def render_hot(cur, budget_tokens: int = HOT_BUDGET_TOKENS) -> tuple[str, bool]:
    """Возвращает (текст, overflowed). При переполнении бюджета — деградация по
    важности (§1.2): эпизоды сначала схлопываются в счётчик, заметки — в заголовки
    (уже заголовки по построению здесь), затем режутся с конца по разделам низкого
    приоритета. Возвращает overflowed=True, если пришлось урезать — вызывающий
    обязан отразить это в meta (C1/health-check)."""
    sections = {
        "Проблемы": _hot_problems(cur),
        "Рекомендации": _hot_recommendations(cur),
        "Эпизоды": _hot_episodes(cur),
        "Открытые вопросы": _hot_open_questions(cur),
        "События": _hot_last_visit_and_labs(cur),
        "Заметки": _hot_notes(cur),
    }
    track = _track_record_digest(cur)
    if track:
        sections["Трек"] = [track]

    def render(secs: dict[str, list[str]]) -> str:
        parts = []
        for label, items in secs.items():
            if items:
                parts.append(f"{label}: " + "; ".join(items))
        return "\n".join(parts) if parts else "(горячий слой пуст)"

    text = render(sections)
    overflowed = False
    if _num_tokens(text) > budget_tokens:
        overflowed = True
        # деградация: эпизоды -> счётчик
        if sections.get("Эпизоды"):
            sections["Эпизоды"] = [f"{len(sections['Эпизоды'])} эпизодов (детали по retrieval)"]
        text = render(sections)
    if _num_tokens(text) > budget_tokens:
        # всё ещё много — режем секции с конца по приоритету (Заметки первыми)
        for key in ["Заметки", "Открытые вопросы", "События"]:
            if _num_tokens(text) <= budget_tokens:
                break
            sections.pop(key, None)
            text = render(sections)

    body = f"[ГОРЯЧЕЕ · карта {datetime.now(timezone.utc).date()}]\n{text}"
    return body, overflowed


# --- 3. Entity index (§4.1) --------------------------------------------------

def index_entity(cur, entity_type: str, entity_value: str, object_id: str, object_type: str, weight: float = 1.0):
    cur.execute(
        sql.SQL("INSERT INTO {t} (entity_type, entity_value, object_id, object_type, weight) VALUES (%s,%s,%s,%s,%s)")
        .format(t=sql.Identifier(schema(), "entity_index")),
        (entity_type, entity_value, object_id, object_type, weight),
    )


# --- 4. Retrieval: L1 словарь, L2 LLM-fallback (§4.2-4.3) -------------------

def resolve_entities_l1(text: str) -> list[tuple[str, str]]:
    lowered = text.lower()
    hits = []
    for phrase, (etype, canonical) in CANONICAL_ENTITIES.items():
        if phrase in lowered and (etype, canonical) not in hits:
            hits.append((etype, canonical))
    return hits


_L2_PROMPT = (
    "Извлеки медицинские сущности из вопроса пациента для поиска в его карте. "
    "Верни JSON {\"entities\": [{\"type\":\"symptom|substance|problem|lab_marker|metric|context\",\"value\":\"английский_ключ\"}]}. "
    "Если сущностей нет — {\"entities\": []}. Только JSON."
)


def resolve_entities_l2_llm(text: str, timeout: float = 8.0) -> list[tuple[str, str]]:
    """Вызывается ТОЛЬКО если L1 ничего не нашёл (дорогой путь — реальный LLM-вызов,
    по замеру этой же сессии ~10-15 сек). Не вызывать из горячего пути диалога без
    необходимости."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return []
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "messages": [{"role": "system", "content": _L2_PROMPT}, {"role": "user", "content": text}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
        return [(e["type"], e["value"]) for e in parsed.get("entities", []) if e.get("type") and e.get("value")]
    except Exception:
        return []  # L2 — best-effort; провал деградирует к "не нашли", не роняет вызов


def retrieve_cold(cur, entities: list[tuple[str, str]], limit_per_entity: int = TIER1_MAX_OBJECTS) -> dict:
    """Возвращает {found: [...], not_found: [...], items: [tier-1 рендеры]}."""
    found, not_found, items = [], [], []
    for etype, evalue in entities:
        cur.execute(
            sql.SQL(
                "SELECT object_id, object_type, weight FROM {t} WHERE entity_type = %s AND entity_value = %s "
                "ORDER BY weight DESC LIMIT %s"
            ).format(t=sql.Identifier(schema(), "entity_index")),
            (etype, evalue, limit_per_entity + 1),
        )
        rows = cur.fetchall()
        if not rows:
            not_found.append(evalue)
            continue
        found.append(evalue)
        if len(rows) > AGGREGATE_THRESHOLD:
            items.append({"kind": "aggregate", "entity": evalue, "count": len(rows),
                          "sample": [r[0] for r in rows[:2]]})
        else:
            for object_id, object_type, _w in rows[:TIER1_MAX_OBJECTS]:
                items.append(tier1_render(cur, object_type, object_id))
    return {"found": found, "not_found": not_found, "items": items}


def tier1_render(cur, object_type: str, object_id: str) -> dict:
    """Сжатая карточка объекта — то, что уходит в контекст по умолчанию (rehydration
    полного объекта — отдельным вызовом get_object, §4.4)."""
    if object_type == "fact":
        cur.execute(
            sql.SQL("SELECT metric_key, value_text, value_num, ts_event FROM {t} WHERE id = %s")
            .format(t=sql.Identifier(schema(), "fact")), (object_id,))
        row = cur.fetchone()
        if row:
            return {"id": object_id, "kind": "fact", "tier": 1,
                    "rendered": f"{row[0]}: {row[2] if row[2] is not None else row[1]} ({row[3].date()})"}
    elif object_type == "episode":
        cur.execute(
            sql.SQL("SELECT symptom_key, status, ts_event, end_ts FROM {t} WHERE id = %s")
            .format(t=sql.Identifier(schema(), "episode")), (object_id,))
        row = cur.fetchone()
        if row:
            return {"id": object_id, "kind": "episode", "tier": 1,
                    "rendered": f"{row[0]} {row[1]} (с {row[2].date()}" + (f" по {row[3].date()}" if row[3] else "") + ")"}
    elif object_type == "memory_note":
        cur.execute(
            sql.SQL("SELECT title, type, content FROM {t} WHERE id = %s")
            .format(t=sql.Identifier(schema(), "memory_note")), (object_id,))
        row = cur.fetchone()
        if row:
            return {"id": object_id, "kind": "memory_note", "tier": 1, "rendered": f"[{row[1]}] {row[0]}"}
    return {"id": object_id, "kind": object_type, "tier": 1, "rendered": f"{object_type} {object_id}"}


def get_object(cur, object_type: str, object_id: str) -> Optional[dict]:
    """Rehydration — полная запись по требованию (§4.4), не в контексте по умолчанию."""
    if object_type not in {"fact", "episode", "problem", "intervention", "recommendation",
                            "visit", "lab_result", "memory_note"}:
        return None
    cur.execute(
        sql.SQL("SELECT * FROM {t} WHERE id = %s").format(t=sql.Identifier(schema(), object_type)),
        (object_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    colnames = [d.name for d in cur.description]
    result = dict(zip(colnames, row))
    for k, v in result.items():
        if isinstance(v, datetime):
            result[k] = v.isoformat()
    return result


def touch_access(cur, object_type: str, object_id: str, reason: str):
    """§8: last_accessed/access_count — метаданные, НЕ журнал (доступ не есть
    изменение истины). Пока применимо только к memory_note (единственный объект
    со столбцами last_accessed/access_count в схеме)."""
    if object_type != "memory_note":
        return
    cur.execute(
        sql.SQL("UPDATE {t} SET last_accessed = now(), access_count = access_count + 1 WHERE id = %s")
        .format(t=sql.Identifier(schema(), "memory_note")),
        (object_id,),
    )


# --- 5. get_context() — контракт §5 -----------------------------------------

def get_context(cur, mode: str, payload: Optional[dict] = None, budget: int = TIER1_BUDGET_TOKENS) -> dict:
    """mode: question | watchdog | health_check | vitrine. C1: missing[] обязателен
    ВСЕГДА (даже когда поиск не выполнялся — тогда searched=[], это ЧЕСТНО другое
    состояние, чем "искали и не нашли"). M4: браслет — всегда, целиком, без
    исключений, для любого режима — этот код не даёт способа его пропустить."""
    bracelet = render_bracelet(cur)
    hot, overflowed = render_hot(cur)

    text = (payload or {}).get("text", "") or ""
    entities: list[tuple[str, str]] = []
    l2_used = False
    if text:
        entities = resolve_entities_l1(text)
        if not entities:
            entities = resolve_entities_l2_llm(text)
            l2_used = True

    cold = {"found": [], "not_found": [], "items": []}
    if entities:
        cold = retrieve_cold(cur, entities)
        for item in cold["items"]:
            if item.get("id"):
                touch_access(cur, item.get("kind"), item["id"], reason=mode)

    searched = [e[1] for e in entities]
    confidence = None
    if searched:
        confidence = 0.6 if l2_used else 1.0  # L1-словарь — точное совпадение; L2-LLM — ниже

    return {
        "bracelet": bracelet,
        "hot": hot,
        "cold": cold["items"],
        "missing": {
            "searched": searched,
            "found": cold["found"],
            "not_found": cold["not_found"],
            "depth_not_loaded": [i["id"] for i in cold["items"] if i.get("tier") == 1 and i.get("id")],
            "extraction_confidence": confidence,
        },
        "meta": {
            "renderer_version": RENDERER_VERSION,
            "hot_overflowed": overflowed,
            "l2_used": l2_used,
            "mode": mode,
        },
    }


# --- 6. memory_note: create для clinical-типа (§2.1, из вердиктов П3) ------

def create_clinical_note(cur, rec_id: str, rec_title: str, verdict: str,
                          metric_key: Optional[str] = None) -> Optional[str]:
    """clinical mn_ — "X не помогает"/"adverse: Y" — автоматически при no_effect/
    adverse (единственный тип заметки, для которого триггер уже полностью
    определён кодом, не ждёт П8/health-check)."""
    if verdict not in ("no_effect", "adverse"):
        return None
    from ulid import ULID
    from app.journal import write_journal

    note_id = f"mn_{ULID()}"
    verdict_word = "не помогает" if verdict == "no_effect" else "adverse-реакция"
    title = f"{rec_title} — {verdict_word}"[:60]
    content = {"rec_id": rec_id, "verdict": verdict, "metric_key": metric_key}
    subject = [{"entity_type": "recommendation", "entity_value": rec_id}]
    if metric_key:
        subject.append({"entity_type": "metric", "entity_value": metric_key})

    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, provenance, verification, type, title, content, subject, source_refs) "
            "VALUES (%s, %s, 'auto', 'clinical', %s, %s, %s, %s)"
        ).format(t=sql.Identifier(schema(), "memory_note")),
        (note_id, json.dumps({"origin": "verdict_engine"}), title, json.dumps(content),
         json.dumps(subject), json.dumps([rec_id])),
    )
    write_journal(cur, "memory_note", note_id, "create",
                  diff={"type": "clinical", "title": title, "rec_id": rec_id, "verdict": verdict},
                  link_back=True)
    return note_id


# --- 7. Забывание — предархивная проверка, слой 1 (§6.2) --------------------

# MVP-словарь критических паттернов — та же логика, что бracelet-keywords: слой 1,
# детерминированный. Слой 2 (LLM structured-output "содержит ли критичное?") —
# сознательно НЕ подключён здесь: это фоновая периодическая проверка, не разговор
# с пациентом; добавлять живой LLM-вызов в неё без явного решения о расписании и
# бюджете — рано. TODO явный, не скрытый.
CRITICAL_FORGET_KEYWORDS = ["аллерг", "анафилакс", "непереносим", "отёк квинке", "анафилактич"]


def run_pre_archive_check(cur) -> list[dict]:
    """Кандидат: эпизод не в active problem, последнее обращение >90 дней назад.
    Слой 1: словарь критических паттернов в symptom_key/context/triggers.
    Возвращает список решений — вызывающий (health-check/watchdog, ещё не
    подключён к расписанию) решает, поднимать ли W3-вопрос или дать уйти в холод
    молча."""
    cur.execute(
        sql.SQL(
            "SELECT id, symptom_key, context, triggers FROM {t} "
            "WHERE status != 'open' AND problem_id IS NULL "
            "AND (end_ts IS NOT NULL AND end_ts < now() - interval '90 days')"
        ).format(t=sql.Identifier(schema(), "episode")),
    )
    out = []
    for ep_id, symptom_key, context, triggers in cur.fetchall():
        text = f"{symptom_key} {context or ''} {' '.join(triggers or [])}".lower()
        critical = any(kw in text for kw in CRITICAL_FORGET_KEYWORDS)
        out.append({
            "episode_id": ep_id, "symptom_key": symptom_key,
            "action": "w3_question" if critical else "archived",
            "reason": "критический паттерн в описании" if critical else "90+ дней тишины, не критично",
        })
    return out
