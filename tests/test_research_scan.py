"""«Научный контур» (2026-09-25) — app/research_scan.py. Юниты на структурный
разбор метаданных (design_type/phase/n — НИКОГДА из LLM, см. докстринг модуля),
дедуп по DOI против реальной card.publication (изолировано), формат дайджеста,
догоняющий прогон пропущенного тика. httpx мокается — сеть не трогаем."""
import httpx
import pytest

from app import notify, research_scan as rs
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _resp(json_body=None, content=None, status_code=200):
    return httpx.Response(request=httpx.Request("GET", "http://test/"), status_code=status_code,
                           json=json_body, content=content)


# ─────── design_type/phase/n — структурно, не LLM ───────

def test_pubmed_design_type_meta_analysis():
    assert rs._pubmed_design_type(["Journal Article", "Meta-Analysis"]) == "meta-analysis"


def test_pubmed_design_type_rct():
    assert rs._pubmed_design_type(["Randomized Controlled Trial"]) == "rct"


def test_pubmed_design_type_case_report():
    assert rs._pubmed_design_type(["Case Reports"]) == "case-report"


def test_pubmed_design_type_unknown_is_honest_none():
    """Не гадаем — "Journal Article"/"Review" в одиночку не маппятся ни на что."""
    assert rs._pubmed_design_type(["Journal Article"]) is None


def test_clinicaltrials_design_type_interventional_is_trial():
    assert rs._clinicaltrials_design_type("INTERVENTIONAL") == "trial"


def test_clinicaltrials_design_type_observational():
    assert rs._clinicaltrials_design_type("OBSERVATIONAL") == "observational"


def test_clinicaltrials_design_type_unknown_is_honest_none():
    assert rs._clinicaltrials_design_type(None) is None


def test_extract_n_finds_narrow_pattern():
    assert rs._extract_n("A cohort of adults (n = 120) was followed for 2 years.") == 120


def test_extract_n_honest_none_when_absent():
    assert rs._extract_n("No sample size mentioned here at all.") is None


def test_extract_n_none_for_empty_text():
    assert rs._extract_n(None) is None
    assert rs._extract_n("") is None


# ─────── fetch_pubmed (httpx мокается, XML — реальная форма E-utilities) ───────

_PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID Version="1">111</PMID>
<Article><Journal><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>
<ArticleTitle>Vitamin D supplementation trial</ArticleTitle>
<Abstract><AbstractText>A randomized trial (n = 240) of vitamin D.</AbstractText></Abstract>
<PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1000/testdoi</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
</PubmedArticleSet>"""


def test_fetch_pubmed_parses_structured_fields(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if "esearch" in url:
            return _resp(json_body={"esearchresult": {"idlist": ["111"]}})
        return _resp(content=_PUBMED_XML)

    monkeypatch.setattr(httpx, "get", fake_get)
    out = rs.fetch_pubmed("vitamin d", 7)
    assert len(out) == 1
    r = out[0]
    assert r["design_type"] == "rct"          # из PublicationType, не выдумано
    assert r["doi"] == "10.1000/testdoi"      # из ELocationID
    assert r["year"] == 2026                  # из PubDate
    assert r["n"] == 240                      # regex-фолбэк по аннотации


def test_fetch_pubmed_empty_search_returns_empty(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(json_body={"esearchresult": {"idlist": []}}))
    assert rs.fetch_pubmed("nonsense query", 7) == []


def test_fetch_pubmed_survives_network_failure(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert rs.fetch_pubmed("x", 7) == []


# ─────── fetch_clinicaltrials (JSON — реальная форма API v2) ───────

def _ct_study(nct_id="NCT01", study_type="INTERVENTIONAL", phases=None, n=80, update="2026-09-20"):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"Study {nct_id}"},
            "statusModule": {"lastUpdatePostDateStruct": {"date": update},
                              "startDateStruct": {"date": "2026-01-15"}},
            "designModule": {"studyType": study_type, "phases": phases or [],
                              "enrollmentInfo": {"count": n}},
            "descriptionModule": {"briefSummary": "Some summary."},
        }
    }


def test_fetch_clinicaltrials_parses_phase_and_n_structurally(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(
        json_body={"studies": [_ct_study(phases=["PHASE2"], n=150)]}))
    out = rs.fetch_clinicaltrials("lumbar disc herniation", 30)
    assert len(out) == 1
    assert out[0]["design_type"] == "trial"
    assert out[0]["phase"] == "PHASE2"   # из designModule.phases, не LLM
    assert out[0]["n"] == 150            # из enrollmentInfo.count, не regex


def test_fetch_clinicaltrials_observational_type():
    study = _ct_study(study_type="OBSERVATIONAL", n=500)
    assert rs._clinicaltrials_design_type(study["protocolSection"]["designModule"]["studyType"]) == "observational"


def test_fetch_clinicaltrials_skips_studies_older_than_window(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(
        json_body={"studies": [_ct_study(update="2020-01-01")]}))
    out = rs.fetch_clinicaltrials("x", 7)
    assert out == []


# ─────── medRxiv (api.medrxiv.org — реальная форма collection[]) ───────

def _medrxiv_item(doi="10.64898/test.1", title="Vitamin D supplementation preprint", published="NA"):
    return {"doi": doi, "title": title, "date": "2026-09-01",
            "abstract": "A preprint about vitamin D supplementation (n = 60).", "published": published}


def test_fetch_medrxiv_window_survives_total_as_string(monkeypatch):
    """Живой сбой 2026-09-27 (issue_log errdedup:research_scan:scheduler):
    medRxiv отдал "total" строкой, `cursor >= total` падал TypeError, весь
    недельный скан обрывался. Регрессия — total-строка не должна ронять
    прогон."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(
        json_body={"collection": [_medrxiv_item()], "messages": [{"total": "1"}]}))
    out = rs.fetch_medrxiv_window(7)
    assert len(out) == 1


def test_fetch_medrxiv_window_stops_when_total_missing(monkeypatch):
    """total отсутствует вовсе (не только не-число) — тоже не должно уйти
    в бесконечный цикл/упасть, честный 0 останавливает пагинацию сразу."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(json_body={"collection": [], "messages": [{}]}))
    assert rs.fetch_medrxiv_window(7) == []


def test_filter_medrxiv_by_topic_matches_title_or_abstract():
    preprints = [_medrxiv_item(), {"doi": "10.64898/other", "title": "Unrelated cardiology study",
                                    "date": "2026-09-01", "abstract": "About heart rhythm disorders.", "published": "NA"}]
    out = rs.filter_medrxiv_by_topic(preprints, "vitamin D supplementation")
    assert len(out) == 1
    assert out[0]["design_type"] == "preprint"
    assert out[0]["n"] == 60
    assert out[0]["url"] == "https://doi.org/10.64898/test.1"


def test_filter_medrxiv_by_topic_skips_items_without_doi():
    preprints = [{"title": "Vitamin D thing", "abstract": "", "date": "2026-09-01", "published": "NA"}]
    assert rs.filter_medrxiv_by_topic(preprints, "vitamin D") == []


# ─────── upsert_publication — дедуп по DOI (акс. критерий: "живым тестом") ───────

_REC = {"source": "pubmed", "external_ref": "pubmed:999", "doi": "10.1000/dedup-test",
        "url": "https://x", "title": "Test dedup article", "abstract_raw": "abs",
        "design_type": "rct", "phase": None, "n": 50, "year": 2026}


def test_upsert_publication_dedups_by_doi():
    with get_conn() as conn, conn.cursor() as cur:
        id1, created1 = rs.upsert_publication(cur, _REC, "test_topic")
        id2, created2 = rs.upsert_publication(cur, dict(_REC, external_ref="pubmed:different"), "test_topic")
        conn.commit()
    assert created1 is True
    assert created2 is False        # тот же DOI — не новая строка
    assert id1 == id2               # одна и та же строка

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.publication WHERE doi = %s", (_REC["doi"],))
        assert cur.fetchone()[0] == 1   # ровно одна строка на DOI, не две


def test_upsert_publication_no_doi_dedups_by_external_ref():
    rec = dict(_REC, doi=None, external_ref="nct:NCT_DEDUP_TEST")
    with get_conn() as conn, conn.cursor() as cur:
        id1, created1 = rs.upsert_publication(cur, rec, "test_topic")
        id2, created2 = rs.upsert_publication(cur, rec, "test_topic")
        conn.commit()
    assert created1 is True and created2 is False and id1 == id2


def test_sync_medrxiv_published_updates_design_type_when_published():
    rec = dict(_REC, source="medrxiv", doi="10.64898/sync-test", external_ref="doi:10.64898/sync-test",
               design_type="preprint")
    with get_conn() as conn, conn.cursor() as cur:
        pub_id, _ = rs.upsert_publication(cur, rec, "test_topic")
        rs._sync_medrxiv_published(cur, {**rec, "_published": "10.1234/published-version"}, pub_id)
        conn.commit()
        cur.execute(f"SELECT design_type FROM {schema()}.publication WHERE id = %s", (pub_id,))
        assert cur.fetchone()[0] == "trial"


def test_sync_medrxiv_published_noop_when_still_na():
    rec = dict(_REC, source="medrxiv", doi="10.64898/na-test", external_ref="doi:10.64898/na-test",
               design_type="preprint")
    with get_conn() as conn, conn.cursor() as cur:
        pub_id, _ = rs.upsert_publication(cur, rec, "test_topic")
        rs._sync_medrxiv_published(cur, {**rec, "_published": "NA"}, pub_id)
        conn.commit()
        cur.execute(f"SELECT design_type FROM {schema()}.publication WHERE id = %s", (pub_id,))
        assert cur.fetchone()[0] == "preprint"


# ─────── build_profile — читает card.research_topic ───────

def test_build_profile_reads_active_topics_only():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO {schema()}.research_topic (topic_key, search_term, label, active) "
                    "VALUES ('active_topic', 'term a', 'Тема А', true), "
                    "('inactive_topic', 'term b', 'Тема Б', false)")
        conn.commit()
        profile = rs.build_profile(cur)
    keys = {p["topic_key"] for p in profile}
    assert "active_topic" in keys
    assert "inactive_topic" not in keys


# ─────── llm_relevance_filter — LLM видит title+abstract, НЕ решает design_type ───────

def test_llm_relevance_filter_parses_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _resp(json_body={
            "choices": [{"message": {"content": '{"items": [{"index": 0, "relevant": true, "why": "важно тебе"}]}'}}],
            "usage": {},
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    result = rs.llm_relevance_filter([{"title": "T", "abstract_raw": "A"}], "Тема")
    assert result == {0: {"relevant": True, "why": "важно тебе"}}
    # промпт не просит design_type/phase у модели — это структурные поля, не её работа
    assert "response_format" in captured["json"]


def test_llm_relevance_filter_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert rs.llm_relevance_filter([{"title": "T", "abstract_raw": "A"}], "Тема") == {}


def test_llm_relevance_filter_empty_candidates_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert rs.llm_relevance_filter([], "Тема") == {}


def test_llm_relevance_filter_survives_model_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rs.llm_relevance_filter([{"title": "T", "abstract_raw": "A"}], "Тема") == {}


# ─────── build_digest_text — Часть 4 (два блока, честная "нет аннотации") ───────

def _item(design_type, title="Title", why="почему", abstract="abs", **kw):
    d = {"design_type": design_type, "title": title, "why_for_you": why, "abstract_raw": abstract,
         "url": "https://x", "year": 2026, "n": None, "phase": None}
    d.update(kw)
    return d


def test_build_digest_text_empty_returns_none():
    assert rs.build_digest_text([]) is None


def test_build_digest_text_splits_validated_and_emerging():
    items = [_item("rct", title="RCT study"), _item("preprint", title="Preprint study")]
    text = rs.build_digest_text(items)
    assert "Проверено" in text
    assert "Перспективно (не внедрено)" in text
    assert "RCT study" in text and "Preprint study" in text
    assert text.index("RCT study") < text.index("Перспективно")
    assert text.index("Preprint study") > text.index("Перспективно")


def test_build_digest_text_marks_missing_abstract_honestly():
    items = [_item("rct", abstract=None)]
    text = rs.build_digest_text(items)
    assert "[нет аннотации]" in text


def test_build_digest_text_discuss_with_doctor_only_validated():
    items = [_item("rct", title="Validated one"), _item("preprint", title="Preprint one")]
    text = rs.build_digest_text(items)
    assert "Что обсудить с доктором" in text
    assert "Validated one" in text.split("Что обсудить с доктором")[1]
    assert "Preprint one" not in text.split("Что обсудить с доктором")[1]


def test_pick_discuss_with_doctor_caps_at_limit():
    items = [_item("rct", title=f"T{i}") for i in range(5)]
    assert len(rs._pick_discuss_with_doctor(items)) == 3


def test_pick_discuss_with_doctor_carries_the_reason():
    """«Стоп-кровь каналов» (2026-09-26, часть 2.4) — строка обязана нести
    причину (why_for_you), не просто имя статьи."""
    items = [_item("rct", title="Vitamin D trial", why="у тебя низкий D")]
    lines = rs._pick_discuss_with_doctor(items)
    assert lines == ["спроси доктора про «Vitamin D trial» — у тебя низкий D"]


def test_pick_discuss_with_doctor_skips_items_without_a_reason():
    """Без причины — честно не включаем в блок вовсе (не пустой шаблон)."""
    items = [_item("rct", title="No reason study", why=None)]
    assert rs._pick_discuss_with_doctor(items) == []


# ─────── _send_digest — сервисный бот напрямую, log_external_send (НЕ notify()) ───────

def test_send_digest_uses_service_bot_and_logs_external(monkeypatch):
    sent = []
    logged = []
    monkeypatch.setattr(rs.service_telegram, "send_message", lambda chat_id, text, **kw: sent.append((chat_id, text)))
    monkeypatch.setattr(rs.notify, "log_external_send", lambda source, priority: logged.append((source, priority)))

    with get_conn() as conn, conn.cursor() as cur:
        pub_id, _ = rs.upsert_publication(cur, _REC, "test_topic")
        conn.commit()

    ok = rs._send_digest([{**_REC, "id": pub_id, "why_for_you": "важно"}])
    assert ok is True
    assert len(sent) == 1 and sent[0][0] == rs.service_telegram.CHAT_ID
    assert logged == [("research_scan", "normal")]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT shown_in_digest FROM {schema()}.publication WHERE id = %s", (pub_id,))
        assert cur.fetchone()[0] is True


def test_send_digest_empty_items_returns_false_no_send(monkeypatch):
    sent = []
    monkeypatch.setattr(rs.service_telegram, "send_message", lambda *a, **k: sent.append(a))
    assert rs._send_digest([]) is False
    assert sent == []


def test_send_digest_send_failure_logs_failed_source(monkeypatch):
    logged = []
    monkeypatch.setattr(rs.service_telegram, "send_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")))
    monkeypatch.setattr(rs.notify, "log_external_send", lambda source, priority: logged.append(source))
    ok = rs._send_digest([{**_REC, "id": "pub_fake"}])
    assert ok is False
    assert logged == ["research_scan_failed"]


# ─────── run_scheduler — догоняющий прогон пропущенного тика (Часть 3.4) ───────

def test_run_scheduler_catches_up_when_last_run_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone
    calls = []
    monkeypatch.setattr(rs.run_log, "last_ok_at", lambda name: datetime.now(timezone.utc) - timedelta(days=10))
    monkeypatch.setattr(rs, "run_once", lambda: calls.append("ran") or {"topics": 0, "new_relevant": 0, "sent": False})
    monkeypatch.setattr(rs.run_log, "mark_run", lambda name: calls.append("marked"))

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()  # прерываем бесконечный while после проверки catch-up
    monkeypatch.setattr(rs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        rs.run_scheduler()
    assert calls == ["ran", "marked"]


def test_run_scheduler_no_catchup_when_recent(monkeypatch):
    from datetime import datetime, timezone
    calls = []
    monkeypatch.setattr(rs.run_log, "last_ok_at", lambda name: datetime.now(timezone.utc))
    monkeypatch.setattr(rs, "run_once", lambda: calls.append("ran") or {"topics": 0, "new_relevant": 0, "sent": False})

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(rs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        rs.run_scheduler()
    assert calls == []  # свежий прогон был — никакого внепланового запуска


def test_run_scheduler_catches_up_when_never_run(monkeypatch):
    calls = []
    monkeypatch.setattr(rs.run_log, "last_ok_at", lambda name: None)
    monkeypatch.setattr(rs, "run_once", lambda: calls.append("ran") or {"topics": 0, "new_relevant": 0, "sent": False})
    monkeypatch.setattr(rs.run_log, "mark_run", lambda name: None)

    def stop_loop(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(rs.timeutil, "sleep_until_local", stop_loop)

    with pytest.raises(KeyboardInterrupt):
        rs.run_scheduler()
    assert calls == ["ran"]
