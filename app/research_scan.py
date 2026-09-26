"""«Научный контур» (2026-09-25) — единственная цель VISION со статусом «ноль»
(G2 «свежая наука, включая фронт»): еженедельный скан PubMed/ClinicalTrials.gov/
medRxiv по темам профиля Влада, честно маркируя зрелость каждой находки
(мета-анализ/РКИ/когорта/фаза/препринт), фундамент для консилиума (без него
консилиум повторяет провал 23.09 — «простыня из ссылок, а не что делать»).

ЖЁСТКИЙ ПРИНЦИП (буквально из тикета): ФАКТЫ — только из структурированных
метаданных API. design_type/phase/n/year — ВСЕГДА из ответа API (PublicationType
в PubMed XML, phase/enrollmentInfo.count в ClinicalTrials.gov JSON, регэксп по
"n = 123" в тексте аннотации как честный fallback, никогда не LLM). LLM
(_llm_relevance_filter) видит title+abstract+design_type/phase/year — только
решает "релевантно ли" и пишет одну строку "почему тебе", НЕ имеет доступа
переопределить design_type/phase сама (её выход не содержит этих полей вообще —
структурно невозможно "додумать" фазу).

Профиль тем — card.research_topic (WHERE active), заполнен ОДНОРАЗОВО из
данных при первом запуске этого тикета (см. AGENT_SYNC), правки — вручную
(NocoDB/SQL), не пере-выводится автоматически на каждом скане: и профиль, и
раздел "Часть 2" тикета описывают ОДНОРАЗОВЫЙ черновик + ручную курацию, не
вечный авто-пересчёт, который бы тихо переписывал то, что Влад поправил.

Источники (без новых сервисов/ключей — все три публичны):
- PubMed E-utilities (esearch -> PMID, efetch -> XML с PublicationType/DOI/Abstract).
- ClinicalTrials.gov API v2 (studies?query.term=... -> JSON, phase/enrollment встроены).
- medRxiv (api.medrxiv.org, НЕ api.biorxiv.org — второй хост отдаёт пустое тело для
  medrxiv-коллекции при живой проверке 2026-09-25, первый работает) — детали-по-дате,
  без keyword-фильтра в самом API, фильтруем по заголовку/аннотации на своей стороне.
  published != "NA" — препринт уже вышел статьёй (см. _sync_medrxiv_published).
"""
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import json
from psycopg import sql
from ulid import ULID

from app import llm_usage, notify, run_log, service_telegram, timeutil
from app.ai_models import DEFAULT_MODEL
from app.db import get_conn, schema
from app.journal import write_journal
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = DEFAULT_MODEL
PROVIDER_ORDER = ["Crusoe", "Fireworks", "BaseTen"]

PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
MEDRXIV_URL = "https://api.medrxiv.org/details/medrxiv/{frm}/{to}/{cursor}"

FIRST_RUN_DAYS_BACK = 30
WEEKLY_DAYS_BACK = 7
MAX_PER_SOURCE_PER_TOPIC = 15
MAX_MEDRXIV_PAGES = 5  # 5*100=500 препринтов/окно — достаточно для клиентского keyword-фильтра
MAX_DIGEST_ITEMS = 10

WEEKLY_HOUR_VL = 21
WEEKLY_MINUTE_VL = 50
WEEKLY_WEEKDAY = 6  # воскресенье (Python Monday=0)

DESIGN_TYPES = ("meta-analysis", "rct", "observational", "case-report", "preprint", "trial")
# "Проверено" — устоявшийся дизайн (синтез/завершённое наблюдение), "Перспективно" —
# ещё не внедрено (испытание любой фазы, препринт, единичный случай). Часть 4 тикета.
_VALIDATED_TYPES = {"meta-analysis", "rct", "observational"}

_GRADE_LABEL = {
    "meta-analysis": "мета-анализ", "rct": "РКИ", "observational": "когорта/наблюдение",
    "case-report": "клинический случай", "preprint": "препринт", "trial": "испытание",
}

_N_RX = re.compile(r"\bn\s*=\s*(\d{1,6})\b", re.I)


def _extract_n(text: Optional[str]) -> Optional[int]:
    """Честный fallback (Часть 1 тикета: "n только если число найдено в аннотации
    или метаданных; не найдено — пусто, не выдумывать") — узкий регэксп "n = 123",
    не эвристика по любому числу в тексте (ложные совпадения хуже пустого поля)."""
    if not text:
        return None
    m = _N_RX.search(text)
    return int(m.group(1)) if m else None


# =====================================================================
# PubMed E-utilities
# =====================================================================

_PUBMED_TYPE_MAP: list[tuple[tuple[str, ...], str]] = [
    (("meta-analysis", "systematic review"), "meta-analysis"),
    (("randomized controlled trial",), "rct"),
    (("clinical trial", "clinical trial, phase i", "clinical trial, phase ii",
      "clinical trial, phase iii", "clinical trial, phase iv", "controlled clinical trial"), "trial"),
    (("observational study", "comparative study", "multicenter study"), "observational"),
    (("case reports",), "case-report"),
]


def _pubmed_design_type(pub_types: list[str]) -> Optional[str]:
    """ИСКЛЮЧИТЕЛЬНО из <PublicationType> (структурное поле PubMed XML) — не LLM,
    не эвристика по заголовку. Порядок проверки — приоритет при нескольких типов
    одновременно (мета-анализ обгоняет "Journal Article" и т.п. общие типы)."""
    lowered = {t.lower() for t in pub_types}
    for needles, design in _PUBMED_TYPE_MAP:
        if lowered & set(needles):
            return design
    return None


def fetch_pubmed(search_term: str, days_back: int, timeout: float = 15.0) -> list[dict]:
    try:
        resp = httpx.get(PUBMED_ESEARCH, params={
            "db": "pubmed", "term": search_term, "reldate": days_back, "datetype": "pdat",
            "retmax": MAX_PER_SOURCE_PER_TOPIC, "retmode": "json",
        }, timeout=timeout)
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

        resp2 = httpx.get(PUBMED_EFETCH, params={
            "db": "pubmed", "id": ",".join(ids), "rettype": "abstract", "retmode": "xml",
        }, timeout=timeout)
        resp2.raise_for_status()
        root = ET.fromstring(resp2.content)
    except Exception:
        logger.exception("research_scan: PubMed недоступен для темы %r — пропускаю источник", search_term)
        return []

    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid_el = art.find(".//PMID")
        pmid = pmid_el.text if pmid_el is not None else None
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else None
        if not pmid or not title:
            continue
        abstract = " ".join(
            "".join(t.itertext()) for t in art.findall(".//Abstract/AbstractText")
        ).strip() or None
        doi = None
        for aid in art.findall(".//ArticleId"):
            if aid.get("IdType") == "doi":
                doi = aid.text
        pub_types = [pt.text for pt in art.findall(".//PublicationTypeList/PublicationType") if pt.text]
        year_el = art.find(".//Article/Journal/JournalIssue/PubDate/Year")
        year = int(year_el.text) if year_el is not None and year_el.text and year_el.text.isdigit() else None

        out.append({
            "source": "pubmed", "external_ref": f"pubmed:{pmid}", "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "title": title, "abstract_raw": abstract,
            "design_type": _pubmed_design_type(pub_types),
            "phase": None, "n": _extract_n(abstract), "year": year,
        })
    return out


# =====================================================================
# ClinicalTrials.gov API v2
# =====================================================================

def _clinicaltrials_design_type(study_type: Optional[str]) -> Optional[str]:
    """ИСКЛЮЧИТЕЛЬНО из designModule.studyType (структурное поле) — "trial" для
    INTERVENTIONAL (фаза — отдельным полем, см. fetch_clinicaltrials), "observational"
    для OBSERVATIONAL. Ни то ни другое — честный None, не гадаем."""
    if study_type == "INTERVENTIONAL":
        return "trial"
    if study_type == "OBSERVATIONAL":
        return "observational"
    return None


def fetch_clinicaltrials(search_term: str, days_back: int, timeout: float = 15.0) -> list[dict]:
    try:
        resp = httpx.get(CLINICALTRIALS_URL, params={
            "query.term": search_term, "pageSize": MAX_PER_SOURCE_PER_TOPIC,
            "sort": "LastUpdatePostDate:desc",
        }, timeout=timeout)
        resp.raise_for_status()
        studies = resp.json().get("studies", [])
    except Exception:
        logger.exception("research_scan: ClinicalTrials.gov недоступен для темы %r — пропускаю источник", search_term)
        return []

    cutoff = timeutil.now_local().date() - timedelta(days=days_back)
    out = []
    for st in studies:
        proto = st.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        nct_id = ident.get("nctId")
        title = ident.get("briefTitle")
        if not nct_id or not title:
            continue
        status = proto.get("statusModule", {})
        last_update = (status.get("lastUpdatePostDateStruct") or {}).get("date")
        try:
            last_update_date = datetime.strptime(last_update[:10], "%Y-%m-%d").date() if last_update else None
        except ValueError:
            last_update_date = None
        if last_update_date and last_update_date < cutoff:
            continue  # старее окна скана — не новое/не изменившееся с прошлого раза

        design = proto.get("designModule", {})
        phases = design.get("phases") or []
        n = (design.get("enrollmentInfo") or {}).get("count")
        start_date = (status.get("startDateStruct") or {}).get("date", "")
        year = int(start_date[:4]) if start_date[:4].isdigit() else None
        abstract = (proto.get("descriptionModule") or {}).get("briefSummary")

        out.append({
            "source": "clinicaltrials", "external_ref": f"nct:{nct_id}", "doi": None,
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
            "title": title, "abstract_raw": abstract,
            "design_type": _clinicaltrials_design_type(design.get("studyType")),
            "phase": ",".join(phases) if phases else None,
            "n": int(n) if isinstance(n, int) else None, "year": year,
        })
    return out


# =====================================================================
# medRxiv (api.medrxiv.org — см. докстринг модуля про хост)
# =====================================================================

def fetch_medrxiv_window(days_back: int, timeout: float = 15.0) -> list[dict]:
    """Один прогон на ВСЁ окно (не на тему) — API отдаёт по дате, без keyword-
    фильтра; filter_medrxiv_by_topic() ниже фильтрует client-side. Пагинация
    курсором до MAX_MEDRXIV_PAGES (не весь `total`, если он огромный — компромисс
    "прототип важнее идеальности")."""
    today = timeutil.now_local().date()
    frm = (today - timedelta(days=days_back)).isoformat()
    to = today.isoformat()
    out = []
    cursor = 0
    for _ in range(MAX_MEDRXIV_PAGES):
        try:
            resp = httpx.get(MEDRXIV_URL.format(frm=frm, to=to, cursor=cursor), timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("research_scan: medRxiv недоступен (окно %s..%s, cursor=%d) — пропускаю остаток", frm, to, cursor)
            break
        collection = data.get("collection") or []
        out.extend(collection)
        msg = (data.get("messages") or [{}])[0]
        total = msg.get("total", 0)
        cursor += len(collection)
        if not collection or cursor >= total:
            break
    return out


def filter_medrxiv_by_topic(preprints: list[dict], search_term: str) -> list[dict]:
    """Client-side keyword-фильтр (см. докстринг модуля — API не умеет по теме).
    Простое вхождение подстроки без учёта регистра по каждому слову search_term
    длиной >=4 — тот же MVP-уровень, что CONTRA_SYNONYMS/topic_key в других модулях
    этой сессии, не полноценный поиск."""
    words = [w.lower() for w in re.split(r"\s+", search_term) if len(w) >= 4]
    if not words:
        return []
    out = []
    for p in preprints[:500]:
        haystack = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
        if any(w in haystack for w in words):
            doi = p.get("doi")
            if not doi:
                continue
            year_str = str(p.get("date", ""))[:4]
            out.append({
                # doi.org резолвит любой валидный DOI на реальную страницу препринта
                # независимо от префикса (medRxiv сменил 10.1101 -> 10.64898 в какой-то
                # момент — жёстко собирать medrxiv.org/content/... URL из префикса
                # DOI больше нельзя, живая проверка 2026-09-25 поймала это сразу).
                "source": "medrxiv", "external_ref": f"doi:{doi}", "doi": doi,
                "url": f"https://doi.org/{doi}",
                "title": p.get("title"), "abstract_raw": p.get("abstract"),
                "design_type": "preprint", "phase": None,
                "n": _extract_n(p.get("abstract")), "year": int(year_str) if year_str.isdigit() else None,
                "_published": p.get("published"),  # "NA" | реальный DOI — см. _sync_medrxiv_published
            })
            if len(out) >= MAX_PER_SOURCE_PER_TOPIC:
                break
    return out


# =====================================================================
# card.publication — дедуп/запись
# =====================================================================

def upsert_publication(cur, rec: dict, topic_key: str) -> tuple[str, bool]:
    """Дедуп по provenance->>'source_ref' — тот же UNIQUE-паттерн, что уже у
    card.recommendation/expectation/lab_result/intervention (ON CONFLICT DO
    NOTHING RETURNING id). source_ref = DOI, когда он есть (буквальное "один DOI
    дважды — одна строка" из тикета); иначе внешний id источника (nct:.../
    pubmed:... без DOI) — тоже честный уникальный ключ, просто не DOI."""
    table = sql.Identifier(schema(), "publication")
    source_ref = f"doi:{rec['doi']}" if rec.get("doi") else rec["external_ref"]
    new_id = f"pub_{ULID()}"
    provenance = json.dumps({"origin": "research_scan", "source_ref": source_ref, "topic_key": topic_key})

    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, ts_event, provenance, verification, source, doi, url, title, abstract_raw, "
            "design_type, phase, n, year, topic_key) "
            "VALUES (%s, now(), %s, 'auto', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ((provenance->>'source_ref')) DO NOTHING RETURNING id"
        ).format(t=table),
        (new_id, provenance, rec["source"], rec.get("doi"), rec.get("url"), rec["title"], rec.get("abstract_raw"),
         rec.get("design_type"), rec.get("phase"), rec.get("n"), rec.get("year"), topic_key),
    )
    row = cur.fetchone()
    if row:
        write_journal(cur, "publication", row[0], "create",
                      diff={"source": rec["source"], "title": rec["title"], "design_type": rec.get("design_type"),
                            "topic_key": topic_key, "source_ref": source_ref})
        return row[0], True

    cur.execute(
        sql.SQL("SELECT id FROM {t} WHERE provenance->>'source_ref' = %s").format(t=table),
        (source_ref,),
    )
    return cur.fetchone()[0], False


def _sync_medrxiv_published(cur, rec: dict, pub_id: str) -> None:
    """Часть 1 тикета: "Preprint позже вышел как статья — обновить существующую
    строку, не плодить дубль (если дёшево)". medRxiv API отдаёт это прямо в
    поле "published" (реальный DOI статьи, либо "NA") — дёшево, не нужен
    отдельный запрос/эвристика. Строку не дублируем — просто выставляем
    design_type журналу известной статьи."""
    published = rec.get("_published")
    if not published or published == "NA":
        return
    table = sql.Identifier(schema(), "publication")
    cur.execute(
        sql.SQL("UPDATE {t} SET design_type = 'trial', doi = COALESCE(doi, %s) "
                "WHERE id = %s AND design_type = 'preprint'").format(t=table),
        (published, pub_id),
    )
    if cur.rowcount:
        write_journal(cur, "publication", pub_id, "update",
                      diff={"design_type": "trial", "reason": f"medRxiv published_doi={published}"})


# =====================================================================
# LLM-фильтр релевантности (Часть 3.3) — ТОЛЬКО релевантно/нет + одна строка,
# НЕ фазы/дизайн/n (структурно недоступны модели, см. докстринг модуля).
# =====================================================================

_RELEVANCE_SYSTEM_PROMPT = """Ты фильтруешь научные публикации для персонального научного дайджеста пациента.
Тебе дан профиль-тема пациента и список публикаций (заголовок + аннотация, если есть).
Для КАЖДОЙ публикации реши: релевантна ли она этой теме пациента, и если да — одна строка "почему это важно ЕМУ" (не общий пересказ абстракта, а привязка к его теме).
НЕ пиши про дизайн исследования, фазу испытания, размер выборки — эти данные тебе не нужны и не должны попадать в твой ответ, они уже известны из метаданных.
Если у публикации нет аннотации — оцени релевантность только по заголовку, честно, не выдумывай содержимое.
Верни JSON строго по схеме:
{"items": [{"index": int, "relevant": bool, "why": string}]}
why — пусто, если relevant=false. Только JSON, без пояснений."""


def llm_relevance_filter(candidates: list[dict], topic_label: str, timeout: float = 30.0) -> dict[int, dict]:
    """candidates — список {title, abstract_raw}, возвращает {index: {"relevant":bool,"why":str}}.
    Пустой ответ модели/сбой -> {} (честно "не оценено", вызывающий не должен
    падать на этом — публикация остаётся в базе без relevant, скан продолжается)."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key or not candidates:
        return {}
    items_text = "\n\n".join(
        f"[{i}] Заголовок: {c['title']}\nАннотация: {c.get('abstract_raw') or 'нет аннотации'}"
        for i, c in enumerate(candidates)
    )
    user_content = f"Тема пациента: {topic_label}\n\nПубликации:\n{items_text}"
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": MODEL, "temperature": 0,
                "provider": {"order": PROVIDER_ORDER, "allow_fallbacks": True},
                "messages": [
                    {"role": "system", "content": _RELEVANCE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        llm_usage.record("research_scan", MODEL, data.get("usage"))
        parsed = json.loads(data["choices"][0]["message"]["content"])
        return {
            item["index"]: {"relevant": bool(item.get("relevant")), "why": str(item.get("why") or "")[:300]}
            for item in parsed.get("items", []) if isinstance(item.get("index"), int)
        }
    except Exception:
        logger.exception("research_scan: LLM-фильтр релевантности упал для темы %r", topic_label)
        return {}


# =====================================================================
# Профиль тем (Часть 2) — читает card.research_topic, НЕ пересчитывает
# (см. докстринг модуля).
# =====================================================================

def build_profile(cur) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT topic_key, search_term, label FROM {t} WHERE active ORDER BY topic_key")
        .format(t=sql.Identifier(schema(), "research_topic")),
    )
    return [{"topic_key": k, "search_term": s, "label": l or s} for k, s, l in cur.fetchall()]


# =====================================================================
# Оркестрация скана
# =====================================================================

def _days_back_for_topic(cur, topic_key: str) -> int:
    cur.execute(
        sql.SQL("SELECT 1 FROM {t} WHERE topic_key = %s LIMIT 1").format(t=sql.Identifier(schema(), "publication")),
        (topic_key,),
    )
    return WEEKLY_DAYS_BACK if cur.fetchone() else FIRST_RUN_DAYS_BACK


def scan_topic(cur, topic: dict, medrxiv_window: list[dict]) -> list[dict]:
    """Возвращает НОВЫЕ (только что вставленные) публикации этой темы с уже
    посчитанным LLM-вердиктом релевантности — то, что может попасть в дайджест.
    medrxiv_window — общий на весь скан (см. run_once): API отдаёт по дате, не
    по теме, фильтровать имеет смысл один раз на shared-список, а не тянуть
    те же ~500 препринтов заново на КАЖДУЮ из 10 тем (было бы 10x лишних
    HTTP-запросов на один и тот же контент — поймано при первом живом прогоне)."""
    topic_key, search_term, label = topic["topic_key"], topic["search_term"], topic["label"]
    days_back = _days_back_for_topic(cur, topic_key)

    records = fetch_pubmed(search_term, days_back) + fetch_clinicaltrials(search_term, days_back)
    records += filter_medrxiv_by_topic(medrxiv_window, search_term)

    new_rows = []
    for rec in records:
        pub_id, created = upsert_publication(cur, rec, topic_key)
        if rec["source"] == "medrxiv":
            _sync_medrxiv_published(cur, rec, pub_id)
        if created:
            new_rows.append({**rec, "id": pub_id})

    if not new_rows:
        return []

    verdicts = llm_relevance_filter(
        [{"title": r["title"], "abstract_raw": r.get("abstract_raw")} for r in new_rows], label,
    )
    out = []
    pub_table = sql.Identifier(schema(), "publication")
    for i, r in enumerate(new_rows):
        v = verdicts.get(i, {})
        cur.execute(
            sql.SQL("UPDATE {t} SET relevant = %s, why_for_you = %s WHERE id = %s").format(t=pub_table),
            (v.get("relevant"), v.get("why") or None, r["id"]),
        )
        if v.get("relevant"):
            out.append({**r, "why_for_you": v.get("why") or "", "topic_label": label})
    return out


def run_once() -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        profile = build_profile(cur)
        conn.commit()

    if not profile:
        logger.warning("research_scan: card.research_topic пуст (или все active=false) — сканировать нечего")
        return {"topics": 0, "new_relevant": 0, "sent": False}

    with get_conn() as conn, conn.cursor() as cur:
        max_days_back = max(_days_back_for_topic(cur, t["topic_key"]) for t in profile)
    medrxiv_window = fetch_medrxiv_window(max_days_back)

    relevant: list[dict] = []
    for topic in profile:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                found = scan_topic(cur, topic, medrxiv_window)
                conn.commit()
            relevant.extend(found)
        except Exception:
            logger.exception("research_scan: тема %r упала — продолжаю остальные", topic["topic_key"])

    sent = False
    if relevant:
        sent = _send_digest(relevant)
    else:
        logger.info("research_scan: новых релевантных публикаций на этой неделе нет — дайджест не отправлен")
    return {"topics": len(profile), "new_relevant": len(relevant), "sent": sent}


# =====================================================================
# Дайджест (Часть 4) — два блока, императивные "что обсудить", отдельным
# сообщением сервисным ботом (НЕ через notify()/общий вечерний дайджест —
# см. границы тикета; тот же паттерн, что anamnesis/nutrition_reports, только
# по воскресеньям).
# =====================================================================

def _format_item(r: dict) -> str:
    grade = _GRADE_LABEL.get(r.get("design_type"), r.get("design_type") or "тип не определён")
    if r.get("phase"):
        grade = f"{grade} ({r['phase']})"
    bits = [grade]
    if r.get("n"):
        bits.append(f"n={r['n']}")
    if r.get("year"):
        bits.append(str(r["year"]))
    meta = " · ".join(bits)
    # Часть 1 тикета: "нет аннотации — позиция идёт с меткой «нет аннотации»,
    # выдумывать содержимое запрещено" — LLM уже видела это (промпт велит судить
    # по заголовку и честно), здесь только видимая пометка, why_for_you не трогаем.
    why = r.get("why_for_you") or "(без обоснования)"
    if not r.get("abstract_raw"):
        why += " [нет аннотации]"
    return f"• {r['title']}\n  почему тебе: {why}\n  {meta} — {r.get('url') or ''}"


def build_digest_text(items: list[dict]) -> Optional[str]:
    if not items:
        return None
    items = items[:MAX_DIGEST_ITEMS]
    validated = [r for r in items if r.get("design_type") in _VALIDATED_TYPES]
    emerging = [r for r in items if r.get("design_type") not in _VALIDATED_TYPES]

    parts = ["🔬 Научный дайджест недели"]
    if validated:
        parts.append("Проверено:\n" + "\n\n".join(_format_item(r) for r in validated))
    if emerging:
        parts.append("Перспективно (не внедрено):\n" + "\n\n".join(_format_item(r) for r in emerging))

    discuss = _pick_discuss_with_doctor(items)
    if discuss:
        parts.append("Что обсудить с доктором:\n" + "\n".join(f"- {d}" for d in discuss))
    return "\n\n———\n\n".join(parts)


def _pick_discuss_with_doctor(items: list[dict], limit: int = 3) -> list[str]:
    """0-3 императивных строки — только для validated-грейда (мета-анализ/РКИ/
    когорта), не для препринтов/испытаний ранней фазы (Часть 4: "не внедрено" —
    рано что-то менять по одному препринту).

    «Стоп-кровь каналов» (2026-09-26, часть 2.4): раньше строка была шаблоном
    "спроси про «X»" без единого слова причины — не вопрос и не совет, просто
    имя статьи. Теперь строка ЛИБО несёт причину (используем уже посчитанный
    why_for_you — не выдумываем новую), ЛИБО публикация честно не попадает в
    этот блок вовсе (сам текст статьи уже виден в "Проверено" выше — тут
    только повод завести разговор, без повода заводить нечего)."""
    out = []
    for r in items:
        if r.get("design_type") not in _VALIDATED_TYPES:
            continue
        why = r.get("why_for_you")
        if not why:
            continue
        out.append(f"спроси доктора про «{r['title'][:70]}» — {why}")
        if len(out) >= limit:
            break
    return out


def _send_digest(items: list[dict]) -> bool:
    text = build_digest_text(items)
    if not text:
        return False
    try:
        service_telegram.send_message(service_telegram.CHAT_ID, text)
    except Exception:
        logger.exception("research_scan: не удалось отправить дайджест")
        notify.log_external_send("research_scan_failed", "normal")
        return False
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("UPDATE {t} SET shown_in_digest = true WHERE id = ANY(%s)")
            .format(t=sql.Identifier(schema(), "publication")),
            ([r["id"] for r in items[:MAX_DIGEST_ITEMS]],),
        )
        conn.commit()
    notify.log_external_send("research_scan", "normal")
    return True


# =====================================================================
# Планировщик — воскресенье ~21:50 ВЛ, догоняющий прогон пропущенного тика
# (Часть 3.4: "молчание недельного цикла невозможно").
# =====================================================================

def run_scheduler() -> None:
    logger.info("research_scan scheduler: старт (вс %02d:%02d ВЛ)", WEEKLY_HOUR_VL, WEEKLY_MINUTE_VL)
    last_ok = run_log.last_ok_at("research_scan")
    if last_ok is None or (datetime.now(timezone.utc) - last_ok) > timedelta(days=7):
        logger.info("research_scan: догоняю пропущенный тик (последний успешный прогон %s)", last_ok)
        try:
            run_once()
            run_log.mark_run("research_scan")
        except Exception as e:
            logger.exception("research_scan: догоняющий прогон упал")
            alert_on_failure("research_scan", e)

    while True:
        try:
            timeutil.sleep_until_local(WEEKLY_HOUR_VL, WEEKLY_MINUTE_VL, weekday=WEEKLY_WEEKDAY)
            run_once()
            run_log.mark_run("research_scan")
        except Exception as e:
            logger.exception("research_scan scheduler упал — критический алерт, повтор через час")
            alert_on_failure("research_scan", e)
            time.sleep(3600)
