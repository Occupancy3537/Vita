"""Консилиум специалистов (2026-09-25) — флагманская фича, последний шаг
проекта план CONSILIUM_PLAN_2026-09-23.md (прочитан перед реализацией).

Урок провала 23.09 (прямые слова Влада: «твой консилиум не дал мне ничего
нового... хочется получить не простыню из ссылок, а что делать?») — выдержан
буквально: финальный синтез — короткий императивный список действий
(build_compact_summary), не литературный обзор; «перспективное, ещё не
внедрённое» — отдельный блок с грейдом ИЗ БАЗЫ ПУБЛИКАЦИЙ (card.publication,
структурный design_type/phase — не мнение модели о том, насколько это
многообещающе); хранение — ОТДЕЛЬНАЯ card.consilium_report, не
health.recommendations_log (та же находка, что и в плане: dashboard.py берёт
"план дня" из последней строки recommendations_log без фильтра по типу —
консилиум молча вытеснил бы недельный план).

Каркас card.opinion/card.disagreement был спроектирован (П3), но код никогда
их не писал — этот модуль первый настоящий автор строк там. card.opinion —
одна строка на специалиста (author=специализация, claim, evidence=ссылки на
факты/публикации). card.disagreement — переиспользует существующие колонки
(opinion_doctor/opinion_advisor — исторически "визит живого врача vs наш ИИ",
здесь просто "сторона А/сторона Б" двух специалистов; doctor_had/we_have —
не применимо к внутреннему разногласию специалистов, оставлены пустыми;
significance — что за наблюдение/тест закроет спор, буквально по тикету).
Новая явная связь — report_id (миграция 2026-09-25), а не эти исторические
колонки — под конкретный прогон консилиума.

Пайплайн: подбор 2-4 специальностей (свободно, не зашитый список) -> каждый
специалист (реальный веб-поиск через OpenRouter plugins=[{"id":"web"}], то же
API, никакой новой инфраструктуры) -> обязательный методолог-скептик -> синтез
в короткий список действий, каждое — ЧЕРЕЗ G7 (gates.py, не обходится и не
меняется) -> propose_recommendation() -> либо рекомендация с ожиданием, либо
честный unmeasurable (сама рекомендация решает, дублировать или суперседить
по topic_key — тот же механизм, что и у Weekly Advisor, ничего специального
здесь не потребовалось)."""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from psycopg import sql
from ulid import ULID

from app import llm_usage, notify, run_log, timeutil
from app.db import get_conn, schema
from app.doctor.config import DOCTOR_MODEL, DOCTOR_REASONING_EFFORT
from app.ai_models import DEFAULT_MODEL
from app.journal import write_journal
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]
SELECTOR_MODEL = DEFAULT_MODEL          # простая структурная классификация — дёшево
SPECIALIST_MODEL = DOCTOR_MODEL         # настоящее рассуждение — флагман, не экономим (ТЗ: "LLM-стоимость не ограничение")
SPECIALIST_EFFORT = DOCTOR_REASONING_EFFORT
WEB_SEARCH_MAX_RESULTS = 4
CALL_TIMEOUT_SECONDS = 90.0

MIN_SPECIALISTS = 2
MAX_SPECIALISTS = 4
MAX_ACTIONS = 7
SKEPTIC_ROLE = "методолог-скептик"

MONTHLY_DAY_OF_MONTH = 1
MONTHLY_HOUR_VL = 9
MONTHLY_MINUTE_VL = 30


# =====================================================================
# Часть 1.2 — сбор срезов (расширение досье доктора)
# =====================================================================

def _full_anamnesis(cur) -> list[dict]:
    cur.execute('SELECT "Category", "Question", "Answer" FROM health.anamnesis WHERE "Status" = \'answered\'')
    return [{"category": c, "question": q, "answer": a} for c, q, a in cur.fetchall()]


def _active_problems(cur) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT id, title, icd_hint, opened_ts::date, gate FROM {t} WHERE status = 'active'")
        .format(t=sql.Identifier(schema(), "problem")),
    )
    return [{"id": i, "title": t, "icd_hint": icd, "opened": str(o), "gate": g} for i, t, icd, o, g in cur.fetchall()]


def _lab_dynamics(cur, limit: int = 15) -> list[dict]:
    """Отличие от context.py::_labs_out_of_range (та даёт только последнее
    значение) — здесь вся история маркера, чтобы специалист видел ДИНАМИКУ
    (Часть 1.2 тикета: "лабы вне референса с динамикой"), не точку."""
    cur.execute(
        sql.SQL(
            "SELECT DISTINCT marker_key FROM {t} WHERE value_num IS NOT NULL "
            "AND (ref_min IS NOT NULL OR ref_max IS NOT NULL)"
        ).format(t=sql.Identifier(schema(), "lab_result")),
    )
    out_of_range_keys = set()
    keys = [r[0] for r in cur.fetchall()]
    for key in keys:
        cur.execute(
            sql.SQL("SELECT value_num, ref_min, ref_max FROM {t} WHERE marker_key = %s "
                    "ORDER BY ts_event DESC LIMIT 1").format(t=sql.Identifier(schema(), "lab_result")),
            (key,),
        )
        val, lo, hi = cur.fetchone()
        val_f = float(val)
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi) if hi is not None else None
        if (lo_f is not None and val_f < lo_f) or (hi_f is not None and val_f > hi_f):
            out_of_range_keys.add(key)

    out = []
    for key in list(out_of_range_keys)[:limit]:
        cur.execute(
            sql.SQL("SELECT marker_label, value_num, ts_event::date FROM {t} WHERE marker_key = %s "
                    "ORDER BY ts_event DESC LIMIT 6").format(t=sql.Identifier(schema(), "lab_result")),
            (key,),
        )
        rows = cur.fetchall()
        history = [{"value": float(v), "date": str(d)} for _, v, d in rows]
        out.append({"marker": rows[0][0] or key, "history_recent_first": history})
    return out


def _active_interventions(cur) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT id, kind, name, dose, regimen, started_ts::date FROM {t} WHERE status = 'active'")
        .format(t=sql.Identifier(schema(), "intervention")),
    )
    return [{"id": i, "kind": k, "name": n, "dose": d, "regimen": r, "started": str(s)} for i, k, n, d, r, s in cur.fetchall()]


def _recent_symptoms(cur, topic: str, limit: int = 15) -> list[dict]:
    like = f"%{topic}%"
    cur.execute(
        "SELECT symptom_id, ts::date, symptom, system, severity, status, notes FROM health.symptom_log "
        "WHERE symptom ILIKE %s OR notes ILIKE %s ORDER BY ts DESC LIMIT %s",
        (like, like, limit),
    )
    rows = cur.fetchall()
    if not rows:
        cur.execute(
            "SELECT symptom_id, ts::date, symptom, system, severity, status, notes "
            "FROM health.symptom_log ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [{"id": sid, "date": str(d), "symptom": s, "system": sy, "severity": sev, "status": st, "notes": n}
            for sid, d, s, sy, sev, st, n in rows]


def _relevant_publications(cur, topic: str, limit: int = 8) -> list[dict]:
    """Своя SQL (не app/doctor/tools.py::search_publications) по ЕДИНСТВЕННОЙ
    причине: тот инструмент не отдаёт id (не нужен был доктору в разговоре),
    а здесь id — обязателен: специалист должен цитировать конкретную
    публикацию, а синтез — проставить структурный грейд ("Перспективное"
    из card.publication.design_type/phase, не из мнения модели, см.
    _grade_from_publication) можно только по id, не по заголовку."""
    like = f"%{topic.lower()}%"
    cur.execute(
        sql.SQL(
            "SELECT id, title, design_type, phase, year, n, why_for_you, url FROM {t} "
            "WHERE %s = '' OR lower(title) LIKE %s OR lower(coalesce(abstract_raw, '')) LIKE %s "
            "ORDER BY ts_recorded DESC LIMIT %s"
        ).format(t=sql.Identifier(schema(), "publication")),
        (topic.strip().lower(), like, like, limit),
    )
    return [{"id": i, "title": t, "design_type": dt, "phase": ph, "year": y, "n": n, "why_for_you": w, "url": u}
            for i, t, dt, ph, y, n, w, u in cur.fetchall()]


def _past_consilium_reports(cur, limit: int = 3) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT id, topic, ts_recorded::date, actions FROM {t} WHERE status = 'completed' "
                "ORDER BY ts_recorded DESC LIMIT %s").format(t=sql.Identifier(schema(), "consilium_report")),
        (limit,),
    )
    return [{"id": i, "topic": t, "date": str(d), "actions": a} for i, t, d, a in cur.fetchall()]


def gather_context(cur, topic: str) -> dict:
    """Часть 1.2: полное расширение досье доктора для консилиума. Переиспользует
    существующие приватные хелперы context.py напрямую (не дублирует их SQL) —
    they're module-private by convention, not by Python enforcement; массовое
    переименование под публичное API ради одного нового потребителя сломало бы
    ~20 существующих тестов ради стиля, не по бюджету сложности этого тикета."""
    from app.doctor.context import _labs_out_of_range, _open_investigations, _recent_doctor_notes
    from app.recommendations import get_active_recommendations

    return {
        "anamnesis": _full_anamnesis(cur),
        "active_problems": _active_problems(cur),
        "open_investigations": _open_investigations(cur),
        "labs_out_of_range_now": _labs_out_of_range(cur, limit=20),
        "lab_dynamics": _lab_dynamics(cur),
        "active_recommendations": [r.model_dump() for r in get_active_recommendations()],
        "active_interventions": _active_interventions(cur),
        "recent_symptoms": _recent_symptoms(cur, topic),
        "recent_doctor_notes": _recent_doctor_notes(cur, limit=10),
        "relevant_publications": _relevant_publications(cur, topic),
        "past_consilium_reports": _past_consilium_reports(cur),
    }


# =====================================================================
# Общий LLM-вызов (JSON-режим, опционально веб-поиск)
# =====================================================================

_cost_tracker = threading.local()  # накопитель $ ТЕКУЩЕГО прогона консилиума (per-thread —
# run_consilium() исполняется в фоновом воркере, thread-local не даёт двум
# параллельным прогонам (команда + случайно совпавший месячный тик) смешать суммы.


def _reset_cost_tracker() -> None:
    _cost_tracker.total = 0.0


def _current_run_cost() -> float:
    return getattr(_cost_tracker, "total", 0.0)


def _call_llm(system_prompt: str, user_content: str, module: str, *, model: str = SPECIALIST_MODEL,
              effort: Optional[str] = None, web_search: bool = False, timeout: float = CALL_TIMEOUT_SECONDS) -> dict:
    """Возвращает распарсенный JSON или {} при сбое — честная деградация
    (одна упавшая роль не должна ронять весь консилиум, см. run_consilium).
    Стоимость вызова копится в _cost_tracker (не в возвращаемом значении —
    иначе пришлось бы менять сигнатуру и все места, где _call_llm().get(...)
    читается напрямую)."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {}
    body: dict = {
        "model": model, "temperature": 0.3,
        "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    if effort:
        body["reasoning"] = {"effort": effort}
    if web_search:
        # Часть "Границы" тикета: реальный веб-поиск через плагин OpenRouter —
        # тот же API, что и обычный чат-вызов, новой инфраструктуры не нужно.
        # Живая проверка 2026-09-25: работает, возвращает message.annotations
        # с url_citation (url/title/content) — не используем их структурно
        # (гейты/грейд остаются из card.publication), только как контекст модели.
        body["plugins"] = [{"id": "web", "max_results": WEB_SEARCH_MAX_RESULTS}]
    try:
        resp = httpx.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        llm_usage.record(module, model, usage)
        _cost_tracker.total = _current_run_cost() + float(usage.get("cost") or 0)
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        logger.exception("consilium: LLM-вызов (%s) упал", module)
        return {}


# =====================================================================
# Подбор специалистов — свободно, НЕ зашитый список (Часть 1.3)
# =====================================================================

_SELECTOR_SYSTEM = """Ты помогаешь собрать консилиум врачей для превентивной медицины/долголетия.
По теме/вопросу пациента назови от 2 до 4 наиболее релевантных медицинских специальностей
(например: невролог, ортопед, реабилитолог, кардиолог, липидолог, эндокринолог, гастроэнтеролог,
геронтолог и т.п. — любые, свободно, не ограничивайся списком примеров).
Верни JSON строго по схеме: {"specialists": ["специальность1", "специальность2", ...]}
Только JSON, без пояснений."""


def select_specialists(topic: str, question: Optional[str]) -> list[str]:
    user = f"Тема консилиума: {topic}" + (f"\nВопрос пациента: {question}" if question else "")
    result = _call_llm(_SELECTOR_SYSTEM, user, "consilium_selector", model=SELECTOR_MODEL)
    specialists = [s for s in (result.get("specialists") or []) if isinstance(s, str) and s.strip()]
    specialists = specialists[:MAX_SPECIALISTS] or ["терапевт превентивной медицины", "методолог"]
    if len(specialists) < MIN_SPECIALISTS:
        specialists.append("терапевт превентивной медицины")
    return specialists[:MAX_SPECIALISTS]


# =====================================================================
# Специалист — установленное ОТДЕЛЬНО от перспективного (урок 23.09 #2)
# =====================================================================

_SPECIALIST_SYSTEM = """Ты — {role}, участник консилиума по превентивной медицине/долголетию пациента.
Тебе дано полное досье (анамнез, активные проблемы/расследования, лабы с динамикой, активные
рекомендации/вмешательства, симптомы, релевантные публикации, прошлые консилиумы) и тема/вопрос.

Дай своё профессиональное мнение СТРОГО по своей специальности. Раздели два принципиально разных
статуса доказательности (урок из провала предыдущей попытки — НЕ смешивай их):
- established_actions — то, что уже ДОКАЗАНО и применяется в клинической практике сегодня.
- emerging — то, что прошло только первые фазы исследований с обнадёживающими результатами
  (лекарства ИЛИ методы лечения/реабилитации/диагностики), но ЕЩЁ НЕ стандарт практики.
Каждое действие в established_actions — КОНКРЕТНЫЙ императив (не общие слова "следить за здоровьем"),
с измеримым ожиданием, если оно в принципе есть: metric_key (короткий английский ключ метрики, если
измеримо через часы/лабы/дневник) + direction (up/down) + magnitude (число) + window_days (обычно 7-30),
ЛИБО unmeasurable_reason (честно, если действие в принципе не измеримо числом — визит к врачу и т.п.).
Не выдумывай факты, которых нет в досье — если данных не хватает, так и скажи.
В досье, в relevant_publications, у каждой публикации есть поле id (вида pub_...) — если emerging-метод
опирается на конкретную публикацию оттуда, укажи именно этот id в source; грейд ("фаза/статус") для
"Перспективного" в итоге берётся НЕ из твоего maturity, а структурно из design_type/phase этой публикации —
поэтому без верного id метод останется без грейда.

Верни JSON строго по схеме:
{{
  "claim": "твоё мнение в 1-2 предложения",
  "established_actions": [{{"imperative": "...", "rationale": "...", "metric_key": "..."|null,
                            "direction": "up"|"down"|null, "magnitude": number|null,
                            "window_days": number|null, "unmeasurable_reason": "..."|null}}],
  "emerging": [{{"method": "...", "maturity": "фаза/статус исследований одной фразой",
                "what_is_needed": "что нужно, чтобы это стало применимым",
                "source": "id публикации из досье вида pub_... если метод оттуда, иначе null — НЕ придумывай id"}}],
  "evidence_refs": ["короткие ссылки на факты/публикации/лабы из досье, на которые опираешься"],
  "confidence": number от 0 до 1
}}
Только JSON, без пояснений."""


def run_specialist(role: str, context: dict, topic: str, question: Optional[str]) -> dict:
    system = _SPECIALIST_SYSTEM.format(role=role)
    user = f"Тема: {topic}\n" + (f"Вопрос пациента: {question}\n" if question else "") + \
           f"\nДосье:\n{json.dumps(context, ensure_ascii=False, default=str)}"
    result = _call_llm(system, user, "consilium_specialist", web_search=True, effort=SPECIALIST_EFFORT)
    result.setdefault("claim", "")
    result.setdefault("established_actions", [])
    result.setdefault("emerging", [])
    result.setdefault("evidence_refs", [])
    result.setdefault("confidence", None)
    result["role"] = role
    return result


# =====================================================================
# Методолог-скептик — ОБЯЗАТЕЛЬНАЯ роль (Часть 1.4)
# =====================================================================

_SKEPTIC_SYSTEM = """Ты — методолог-скептик в консилиуме врачей. Твоя работа — критически проверить
выводы коллег-специалистов, а не соглашаться. Ищи конкретно:
- преждевременные выводы (мало данных для такого утверждения),
- путаницу корреляции и причинности,
- устаревшие или единичные данные, выданные за закономерность,
- "простыню" — слишком много несфокусированных действий вместо 1-2 главных,
- действия, для которых нет реального способа проверить эффект.
Не критикуй ради критики — если мнение специалиста обосновано, так и скажи.

Верни JSON строго по схеме:
{"remarks": [{"target": "какого специалиста/какого утверждения касается", "critique": "суть замечания"}],
 "top_concerns": ["1-3 самых важных замечания одной фразой каждое"]}
Только JSON, без пояснений."""


def run_skeptic(specialist_opinions: list[dict], topic: str) -> dict:
    user = f"Тема консилиума: {topic}\n\nМнения специалистов:\n{json.dumps(specialist_opinions, ensure_ascii=False, default=str)}"
    result = _call_llm(_SKEPTIC_SYSTEM, user, "consilium_skeptic", effort=SPECIALIST_EFFORT)
    result.setdefault("remarks", [])
    result.setdefault("top_concerns", [])
    return result


# =====================================================================
# Синтезатор (председатель) — короткий список действий, НЕ обзор (урок #1)
# =====================================================================

_SYNTHESIZER_SYSTEM = """Ты — председатель консилиума. Тебе даны мнения специалистов и замечания
методолога-скептика. Собери ИТОГ в формате, который реально можно использовать — короткий список
действий, а не пересказ мнений (провал предыдущей попытки был ИМЕННО в этом: "простыня из ссылок,
а не что делать").

Правила:
- actions — 0 ДО 7 пунктов, каждый — императив ОДНОЙ строкой + краткое обоснование. Если менять
  нечего — пустой список ЛЕГИТИМЕН, не выдумывай действия ради количества.
- Учти замечания скептика — если он справедливо указал на преждевременность/слабость вывода,
  либо убери такое действие, либо смягчи (перенеси в emerging), либо явно ослабь formulировку.
- emerging — перспективные методы специалистов ОТДЕЛЬНО от actions. Переноси поле source (id публикации
  вида pub_... или null) БУКВАЛЬНО из мнения специалиста, не переписывай и не придумывай — итоговый грейд
  проставляется потом программно из базы публикаций по этому id, не из твоих слов о фазе/статусе.
- disagreements — если специалисты разошлись по существу (не мелочи), опиши: между кем, в чём,
  какое наблюдение или тест разрешит спор. Если разногласий нет — пустой список.
- Каждое действие ЛИБО измеримо (metric_key+direction+magnitude+window_days), ЛИБО
  unmeasurable_reason — без этого действие не может стать рекомендацией (ворота G7).

Верни JSON строго по схеме:
{{"actions": [{{"imperative": "...", "rationale": "...", "metric_key": "..."|null, "direction": "up"|"down"|null,
              "magnitude": number|null, "window_days": number|null, "expectation_type": "delta_abs"|"threshold"|"frequency"|null,
              "freq_min_ratio": number|null, "unmeasurable_reason": "..."|null, "evidence_refs": ["..."]}}],
 "emerging": [{{"method": "...", "maturity": "...", "what_is_needed": "...", "source": "..."}}],
 "disagreements": [{{"between": ["специальность А", "специальность Б"], "about": "...", "resolving_test": "..."}}]}}
Только JSON, без пояснений."""


def synthesize(specialist_opinions: list[dict], skeptic_review: dict, topic: str, question: Optional[str]) -> dict:
    user = (f"Тема: {topic}\n" + (f"Вопрос пациента: {question}\n" if question else "") +
            f"\nМнения специалистов:\n{json.dumps(specialist_opinions, ensure_ascii=False, default=str)}\n\n"
            f"Замечания скептика:\n{json.dumps(skeptic_review, ensure_ascii=False, default=str)}")
    result = _call_llm(_SYNTHESIZER_SYSTEM, user, "consilium_synthesizer", effort=SPECIALIST_EFFORT)
    result.setdefault("actions", [])
    result.setdefault("emerging", [])
    result.setdefault("disagreements", [])
    result["actions"] = result["actions"][:MAX_ACTIONS]
    return result


# =====================================================================
# Персистентность — card.opinion / card.disagreement / рекомендации через G7
# =====================================================================

def _write_opinion(cur, report_id: str, author: str, claim: str, rationale: Optional[str],
                    evidence_refs: list, confidence: Optional[float]) -> str:
    op_id = f"op_{ULID()}"
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, ts_event, provenance, author, claim, rationale, rationale_source, "
            "evidence, confidence, report_id) VALUES (%s, now(), %s, %s, %s, %s, 'llm_consilium', %s, %s, %s)"
        ).format(t=sql.Identifier(schema(), "opinion")),
        (op_id, json.dumps({"origin": "consilium"}), author, claim, rationale,
         json.dumps(evidence_refs, ensure_ascii=False), confidence, report_id),
    )
    write_journal(cur, "opinion", op_id, "create", diff={"author": author, "claim": claim, "report_id": report_id})
    return op_id


def _write_disagreement(cur, report_id: str, between: list, about: str, resolving_test: Optional[str]) -> str:
    dis_id = f"dg_{ULID()}"
    a = between[0] if len(between) > 0 else None
    b = between[1] if len(between) > 1 else None
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, ts_event, provenance, class, opinion_doctor, opinion_advisor, "
            "significance, status, report_id) "
            "VALUES (%s, now(), %s, 'consilium_specialist_disagreement', %s, %s, %s, 'raised', %s)"
        ).format(t=sql.Identifier(schema(), "disagreement")),
        (dis_id, json.dumps({"origin": "consilium"}), a, b, f"{about} — закрывается: {resolving_test or 'не указано'}", report_id),
    )
    write_journal(cur, "disagreement", dis_id, "create", diff={"between": between, "about": about, "report_id": report_id})
    return dis_id


def _publication_id_from_refs(evidence_refs: list) -> Optional[str]:
    """Часть 4.3/«научный контур»: если действие явно опирается на конкретную
    публикацию (evidence_refs содержит её id), связываем — тот же publication_id,
    что уже есть в card.recommendation с прошлого тикета, здесь просто первый
    реальный потребитель поля."""
    for ref in evidence_refs or []:
        ref_str = str(ref)
        if ref_str.startswith("pub_"):
            return ref_str
    return None


def _grade_from_publication(pub_id: Optional[str], publications_by_id: dict) -> tuple[Optional[str], str]:
    """Приёмка тикета буквально: "«Перспективное» отделено, грейды из публикаций,
    не из мнения модели". Грейд ("испытание (фаза 2)" и т.п.) берётся ТОЛЬКО из
    структурных design_type/phase card.publication — тот же _GRADE_LABEL, что
    в еженедельном научном дайджесте (app/research_scan.py), — никогда из
    свободного текста maturity, который пишет специалист. Без совпадения по id
    честно помечаем как неподтверждённое, не подставляем чужой грейд наугад."""
    from app.research_scan import _GRADE_LABEL

    pub = publications_by_id.get(pub_id) if pub_id else None
    if not pub:
        return None, "не подтверждено базой публикаций"
    grade = _GRADE_LABEL.get(pub.get("design_type"), pub.get("design_type") or "тип не определён")
    if pub.get("phase"):
        grade = f"{grade} ({pub['phase']})"
    return grade, "publication"


def _grade_emerging(emerging: list[dict], publications_by_id: dict) -> list[dict]:
    graded = []
    for item in emerging:
        grade, basis = _grade_from_publication(item.get("source"), publications_by_id)
        graded.append({**item, "grade": grade, "grade_basis": basis})
    return graded


def _propose_action_as_recommendation(action: dict, source_ref: str) -> dict:
    """Часть 2.1: каждое действие — ЧЕРЕЗ ворота G7 (gates.py, не обходим,
    не меняем) -> propose_recommendation(). Дубли/суперседд по topic_key —
    существующий механизм sync_recommendation, ничего специального для
    консилиума здесь не потребовалось."""
    from app.recommendations import ProposeRequest, propose_recommendation

    has_metric = bool(action.get("metric_key") and action.get("direction") and action.get("magnitude") is not None)
    req = ProposeRequest(
        title=action["imperative"], action=action["imperative"], rationale=action.get("rationale"),
        kind="consilium", source_ref=source_ref, origin="consilium",
        started_ts=datetime.now(timezone.utc),
        metric_key=action.get("metric_key") if has_metric else None,
        metric_label=action.get("metric_key") if has_metric else None,
        direction=action.get("direction") if has_metric else None,
        magnitude=action.get("magnitude") if has_metric else None,
        window_days=action.get("window_days") or 7,
        expectation_type=action.get("expectation_type") or "delta_abs",
        freq_min_ratio=action.get("freq_min_ratio"),
        unmeasurable_reason=action.get("unmeasurable_reason") if not has_metric else None,
        publication_id=_publication_id_from_refs(action.get("evidence_refs")),
        is_bioage_driver=False, metric_overdue=False,
    )
    resp = propose_recommendation(req)
    return resp.model_dump()


# =====================================================================
# Форматирование итога (Часть 2 — короткий список, НЕ простыня)
# =====================================================================

def build_compact_summary(topic: str, actions_results: list[dict], emerging: list[dict],
                          disagreements: list[dict], skeptic_top: list[str]) -> str:
    parts = [f"🩺 Консилиум: {topic}"]
    if actions_results:
        lines = []
        for a, r in actions_results:
            tag = "✅" if r.get("accepted") else "⛔"
            lines.append(f"{tag} {a['imperative']}")
        parts.append("Действия:\n" + "\n".join(lines))
    else:
        parts.append("Действия: менять нечего — текущий план адекватен.")
    if emerging:
        parts.append("Перспективно (не внедрено):\n" + "\n".join(
            f"- {e['method']} [{e.get('grade') or e.get('grade_basis', '?')}]" for e in emerging[:5]))
    if disagreements:
        parts.append("Разногласия:\n" + "\n".join(f"- {' vs '.join(d.get('between', []))}: {d.get('about', '')}" for d in disagreements))
    if skeptic_top:
        parts.append("Скептик:\n" + "\n".join(f"- {c}" for c in skeptic_top[:3]))
    return "\n\n———\n\n".join(parts)


def build_full_document(topic: str, question: Optional[str], specialist_opinions: list[dict],
                        skeptic_review: dict, synthesis: dict, actions_results: list[dict]) -> str:
    parts = [f"# Консилиум: {topic}"]
    if question:
        parts.append(f"**Вопрос:** {question}")

    action_lines = []
    for a, r in actions_results:
        status = "рекомендация создана" if r.get("accepted") else f"отклонено воротами ({r.get('rejected_gate')}: {r.get('rejected_reason')})"
        action_lines.append(f"- **{a['imperative']}** — {a.get('rationale', '')} _[{status}]_")
    parts.append("## Действия\n" + ("\n".join(action_lines) if action_lines else "Менять нечего — текущий план адекватен."))

    emerging = synthesis.get("emerging") or []
    if emerging:
        # Грейд — СТРУКТУРНЫЙ, из card.publication (_grade_from_publication в
        # run_consilium), не мнение специалиста о фазе/статусе (то maturity —
        # в скобках рядом, для контекста, но не как заявленный грейд).
        parts.append("## Перспективное (не внедрено)\n" + "\n".join(
            f"- **{e['method']}** [грейд: {e.get('grade') or e.get('grade_basis', '?')}]"
            f" (мнение специалиста о статусе: {e.get('maturity', '?')})"
            f" — нужно: {e.get('what_is_needed', '?')} (источник: {e.get('source') or 'не указан'})"
            for e in emerging))

    disagreements = synthesis.get("disagreements") or []
    if disagreements:
        parts.append("## Разногласия\n" + "\n".join(
            f"- {' vs '.join(d.get('between', []))}: {d.get('about', '')} — закрывается: {d.get('resolving_test', '?')}"
            for d in disagreements))

    top_concerns = skeptic_review.get("top_concerns") or []
    if top_concerns:
        parts.append("## Замечания скептика\n" + "\n".join(f"- {c}" for c in top_concerns))

    parts.append("## Мнения специалистов (полностью)")
    for op in specialist_opinions:
        parts.append(f"### {op['role']}\n{op.get('claim', '')}\n\n"
                     f"Уверенность: {op.get('confidence', '?')}\n\nСсылки: {', '.join(op.get('evidence_refs') or [])}")
    return "\n\n".join(parts)


# =====================================================================
# Оркестратор (Часть 1)
# =====================================================================

def run_consilium(topic: str, question: Optional[str] = None, trigger: str = "command") -> dict:
    """Главная точка входа. Возвращает {"report_id", "compact_summary", "full_text", "status"}."""
    t0 = time.monotonic()
    _reset_cost_tracker()
    with get_conn() as conn, conn.cursor() as cur:
        context = gather_context(cur, topic)

    specialists = select_specialists(topic, question)
    logger.info("consilium: тема %r -> специалисты %s", topic, specialists)

    specialist_opinions = []
    for role in specialists:
        try:
            specialist_opinions.append(run_specialist(role, context, topic, question))
        except Exception:
            logger.exception("consilium: специалист %r упал — продолжаю без него", role)
    try:
        skeptic_result = run_skeptic(specialist_opinions, topic)
    except Exception:
        logger.exception("consilium: скептик упал")
        skeptic_result = {"remarks": [], "top_concerns": []}
    skeptic_opinion = {"role": SKEPTIC_ROLE, "claim": "; ".join(skeptic_result.get("top_concerns") or []) or "замечаний нет",
                       "established_actions": [], "emerging": [], "evidence_refs": [], "confidence": None}

    synthesis = synthesize(specialist_opinions, skeptic_result, topic, question)
    publications_by_id = {p["id"]: p for p in (context.get("relevant_publications") or []) if p.get("id")}
    synthesis["emerging"] = _grade_emerging(synthesis.get("emerging") or [], publications_by_id)

    report_id = f"cs_{ULID()}"
    actions_results = []
    with get_conn() as conn, conn.cursor() as cur:
        for op in specialist_opinions + [skeptic_opinion]:
            _write_opinion(cur, report_id, op["role"], op.get("claim", ""), None,
                          op.get("evidence_refs") or [], op.get("confidence"))
        for d in synthesis.get("disagreements") or []:
            _write_disagreement(cur, report_id, d.get("between") or [], d.get("about", ""), d.get("resolving_test"))
        conn.commit()

    for i, action in enumerate(synthesis.get("actions") or []):
        if not action.get("imperative"):
            continue
        try:
            result = _propose_action_as_recommendation(action, source_ref=f"consilium:{report_id}:{i}")
        except Exception:
            logger.exception("consilium: propose_recommendation упал для действия %r", action.get("imperative"))
            result = {"accepted": False, "rejected_gate": "error", "rejected_reason": "внутренняя ошибка"}
        actions_results.append((action, result))

    compact = build_compact_summary(topic, actions_results, synthesis.get("emerging") or [],
                                    synthesis.get("disagreements") or [], skeptic_result.get("top_concerns") or [])
    full_text = build_full_document(topic, question, specialist_opinions + [skeptic_opinion], skeptic_result,
                                    synthesis, actions_results)

    status = "completed" if (actions_results or synthesis.get("emerging")) else "empty"
    cost_usd = round(_current_run_cost(), 6)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {t} (id, topic, question, trigger, roles, actions, emerging, skeptic_notes, "
                "full_text, status, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(t=sql.Identifier(schema(), "consilium_report")),
            (report_id, topic, question, trigger, json.dumps(specialists),
             json.dumps([{"imperative": a["imperative"], "accepted": r.get("accepted"), "id": r.get("id")} for a, r in actions_results], ensure_ascii=False),
             json.dumps(synthesis.get("emerging") or [], ensure_ascii=False),
             json.dumps(skeptic_result.get("top_concerns") or [], ensure_ascii=False),
             full_text, status, cost_usd),
        )
        write_journal(cur, "consilium_report", report_id, "create", diff={"topic": topic, "status": status})
        conn.commit()

    logger.info("consilium: тема %r завершена (%s), %d действий, %.2fs, $%.4f",
                topic, status, len(actions_results), time.monotonic() - t0, cost_usd or 0)
    return {"report_id": report_id, "compact_summary": compact, "full_text": full_text, "status": status}


# СВОЙ executor, не общий с app/doctor/intake.py::_turn_executor (max_workers=1
# там существует, чтобы сохранить ПОРЯДОК обычных ходов диалога одного чата —
# многоминутный консилиум на том же воркере держал бы очередь всех остальных
# сообщений доктору все эти минуты). Один слот — параллельные консилиумы не
# нужны (Влад не запускает несколько сразу), но и с обычным диалогом не спорит.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="consilium")


def _run_command_and_reply(chat_id: str, topic: str) -> None:
    from app.doctor import telegram as doctor_telegram
    try:
        result = run_consilium(topic, trigger="command")
        doctor_telegram.send_message(chat_id, result["compact_summary"])
    except Exception:
        logger.exception("consilium: команда по теме %r упала", topic)
        doctor_telegram.send_message(chat_id, f"Консилиум по теме «{topic}» не получился — техническая ошибка, попробуй ещё раз чуть позже.")


def submit_command(chat_id: str, topic: str) -> None:
    """Единственная точка входа из app/doctor/intake.py (Часть 3.1: команда
    "/консилиум <тема>") — приём + доставка здесь, чтобы intake.py не заводил
    свой executor ради одной фичи."""
    _executor.submit(_run_command_and_reply, chat_id, topic)


# =====================================================================
# Ежемесячный полный консилиум (Часть 3.2) — 1-е число, весь профиль, без
# конкретного вопроса. Догоняющий тик — тот же паттерн, что research_scan.py.
# =====================================================================

def run_monthly() -> dict:
    result = run_consilium("общий профиль долголетия", question=None, trigger="monthly")
    notify.notify("consilium_monthly", "normal", result["compact_summary"])
    return result


# =====================================================================
# Дашборд (Часть 4.1) — ОТДЕЛЬНАЯ секция «Консилиумы», не recommendations_log
# (та же находка, что в плане: dashboard.py берёт "план дня" из последней
# строки recommendations_log без фильтра по типу — консилиум молча вытеснил
# бы недельный план, если бы писал туда же).
# =====================================================================

def get_consilium_reports(cur, limit: int = 20) -> dict:
    cur.execute(
        sql.SQL(
            "SELECT id, ts_recorded, topic, question, trigger, roles, actions, emerging, "
            "skeptic_notes, full_text, status, cost_usd FROM {t} ORDER BY ts_recorded DESC LIMIT %s"
        ).format(t=sql.Identifier(schema(), "consilium_report")),
        (limit,),
    )
    reports = []
    for i, ts, topic, question, trig, roles, actions, emerging, skeptic_notes, full_text, status, cost in cur.fetchall():
        reports.append({
            "id": i, "ts_recorded": ts.isoformat(), "topic": topic, "question": question, "trigger": trig,
            "roles": roles, "actions": actions, "emerging": emerging, "skeptic_notes": skeptic_notes,
            "full_text": full_text, "status": status, "cost_usd": cost,
        })
    return {"reports": reports}


def run_scheduler() -> None:
    logger.info("consilium monthly scheduler: старт (1-е число %02d:%02d ВЛ)", MONTHLY_HOUR_VL, MONTHLY_MINUTE_VL)
    last_ok = run_log.last_ok_at("consilium_monthly")
    if last_ok is None or (datetime.now(timezone.utc) - last_ok) > timedelta(days=32):
        logger.info("consilium: догоняю пропущенный месячный тик (последний успешный прогон %s)", last_ok)
        try:
            run_monthly()
            run_log.mark_run("consilium_monthly")
        except Exception as e:
            logger.exception("consilium: догоняющий месячный прогон упал")
            alert_on_failure("consilium_monthly", e)

    while True:
        try:
            timeutil.sleep_until_local(MONTHLY_HOUR_VL, MONTHLY_MINUTE_VL, day_of_month=MONTHLY_DAY_OF_MONTH)
            run_monthly()
            run_log.mark_run("consilium_monthly")
        except Exception as e:
            logger.exception("consilium monthly scheduler упал — повтор через сутки")
            alert_on_failure("consilium_monthly", e)
            time.sleep(24 * 3600)
