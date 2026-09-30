"""«Проверки» (Vita v2, этап 2, 2026-09-28) — app/checks.py. Использует
health.investigations/anomaly_disposition (реальные, не изолированные по
схеме) для сценария «открытие детектива» — тот же приём, что и
tests/test_detective.py, поэтому переиспользует её вставочные хелперы и
требует ту же фикстуру изоляции."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql
from ulid import ULID

from app import checks
from app.db import get_conn, schema
from tests.test_detective import _insert_episode, _insert_fact, _make_problem

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _t(name):
    return sql.Identifier(schema(), name)


def _write_disagreement(claim_a="гастроэнтеролог", claim_b="диетолог",
                         significance="закрывается: УЗИ желчного пузыря") -> str:
    dis_id = f"dg_{ULID()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, ts_event, provenance, class, opinion_doctor, opinion_advisor, "
                    "significance, status) VALUES (%s, now(), %s, 'test', %s, %s, %s, 'raised')")
            .format(t=_t("disagreement")),
            (dis_id, json.dumps({"origin": "test"}), claim_a, claim_b, significance),
        )
        conn.commit()
    return dis_id


def _notable_alcohol_problem(title: str) -> str:
    """Тот же сценарий, что test_detective.py::test_analyze_problem_notable_finds_alcohol_coincidence —
    гарантированно даёт analyze_problem(status='notable') с фактором «алкоголь», лаг 0."""
    problem_id = _make_problem(title)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, metric_key, metric_label, date, severity, hypotheses) "
                    "VALUES (%s, 'test_metric_chk', 'проверка чек', current_date, 'strong', %s)")
            .format(t=_t("anomaly_disposition")),
            (f"ad_{ULID()}", json.dumps([{"hypothesis": f"{title} от спиртного", "differentiator": None}])),
        )
        conn.commit()
    for off in [10, 12, 14, 16, 18]:
        _insert_episode(problem_id, off, symptom_key=f"test-checks-{problem_id}", context=f"{title} от спиртного")
        _insert_fact("nutrient:Алкоголь", 20.0, off)
    for off in [11, 13, 15, 17, 19]:
        _insert_fact("nutrient:Алкоголь", 0.0, off)
    return problem_id


def _active_recommendation(metric_key="hrv", direction="up", magnitude=5.0, window_days=7,
                           started_days_ago=1, kind=None, origin="advisor", ex_type="delta_abs") -> str:
    from app.recommendations import RecommendationSyncRequest, sync_recommendation

    started = datetime.now(timezone.utc) - timedelta(days=started_days_ago)
    req = RecommendationSyncRequest(
        title="Тестовая проверка checks.py", action="действие", source_ref=f"test-checks:{ULID()}",
        origin=origin, started_ts=started, metric_key=metric_key, metric_label="ВСР", unit="мс",
        direction=direction, magnitude=magnitude, window_days=window_days, lag_days=0, baseline_days=7,
        expectation_type=ex_type, kind=kind,
    )
    resp = sync_recommendation(req)  # открывает и коммитит своё собственное соединение
    return resp.id


def _set_current_verdict(rec_id: str, verdict: str, ts_computed=None) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {t} (id, rec_id, cycle, ts_computed, engine_version, verdict, status) "
                "VALUES (%s, %s, 1, %s, 'test', %s, 'current')"
            ).format(t=_t("recommendation_verdict")),
            (f"rv_{ULID()}", rec_id, ts_computed or datetime.now(timezone.utc), verdict),
        )
        conn.commit()


# ─────── id-ы вопросов ───────

def test_detective_question_id_deterministic():
    a = checks.detective_question_id("pb_ABC", "алкоголь", 0)
    b = checks.detective_question_id("pb_ABC", "алкоголь", 0)
    assert a == b
    assert a.startswith("dq_pb_ABC")


def test_detective_question_id_differs_by_lag():
    a = checks.detective_question_id("pb_ABC", "алкоголь", 0)
    b = checks.detective_question_id("pb_ABC", "алкоголь", 1)
    assert a != b


# ─────── пустая система ───────

def test_list_checks_empty_system():
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur)
    assert result["counts"] == {"questions": 0, "checks": 0, "habits": 0}
    assert result["items"]["questions"] == [] and result["items"]["checks"] == [] and result["items"]["habits"] == []


def test_list_checks_mode_filter_returns_only_that_list():
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="habits")
    assert result["mode"] == "habits"
    assert result["items"] == []
    assert "counts" in result


def test_list_checks_unknown_mode_raises():
    with get_conn() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):
            checks.list_checks(cur, mode="unknown")


# ─────── вопросы: consilium.disagreement ───────

def test_disagreement_appears_as_question():
    dis_id = _write_disagreement()
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="questions")
    qids = [q["id"] for q in result["items"]]
    assert checks.disagreement_question_id(dis_id) in qids
    q = next(q for q in result["items"] if q["id"] == checks.disagreement_question_id(dis_id))
    assert q["source"] == "консилиум" and q["status"] == "ждёт решения"


def test_resolved_disagreement_not_a_question():
    _write_disagreement()
    with get_conn() as conn, conn.cursor() as cur:
        # разногласие со статусом НЕ 'raised' (кем-то/чем-то уже закрыто) не
        # должно попадать в вопросы вовсе — _disagreement_questions фильтрует
        # по status='raised' в самом SQL.
        cur.execute(sql.SQL("UPDATE {t} SET status = 'resolved'").format(t=_t("disagreement")))
        conn.commit()
        result = checks.list_checks(cur, mode="questions")
    assert result["items"] == []


def test_decline_disagreement_removes_it_from_questions_and_creates_no_recommendation():
    dis_id = _write_disagreement()
    qid = checks.disagreement_question_id(dis_id)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.resolve_question(cur, qid, "disagreement", "decline", "Разногласие тест",
                                          reason="не сейчас")
        conn.commit()
    assert result["decision"] == "decline" and result["created_rec_id"] is None
    with get_conn() as conn, conn.cursor() as cur:
        after = checks.list_checks(cur, mode="questions")
        checks_after = checks.list_checks(cur, mode="checks")
    assert qid not in [q["id"] for q in after["items"]]
    assert checks_after["items"] == []


def test_check_disagreement_creates_unmeasurable_recommendation():
    dis_id = _write_disagreement()
    qid = checks.disagreement_question_id(dis_id)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.resolve_question(cur, qid, "disagreement", "check", "УЗИ желчного пузыря",
                                          reason="закрывается: УЗИ")
        conn.commit()
    assert result["created_rec_id"] is not None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT id FROM {t} WHERE rec_id = %s AND type = 'unmeasurable'")
                    .format(t=_t("expectation")), (result["created_rec_id"],))
        assert cur.fetchone() is not None


def test_resolve_already_decided_question_is_idempotent():
    dis_id = _write_disagreement()
    qid = checks.disagreement_question_id(dis_id)
    with get_conn() as conn, conn.cursor() as cur:
        first = checks.resolve_question(cur, qid, "disagreement", "check", "УЗИ", reason="тест")
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        second = checks.resolve_question(cur, qid, "disagreement", "decline", "УЗИ")
    assert second["already_decided"] is True
    assert second["created_rec_id"] == first["created_rec_id"]


# ─────── вопросы: открытие детектива ───────

def test_detective_notable_finding_appears_as_question():
    problem_id = _notable_alcohol_problem("Тест checks детектив альфа")
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="questions")
    matches = [q for q in result["items"] if q.get("problem_id") == problem_id]
    assert matches and matches[0]["source"] == "детектив"
    assert matches[0]["factor"] == "алкоголь" and matches[0]["lag_days"] == 0


def test_check_detective_question_creates_frequency_recommendation_and_registers_metric():
    problem_id = _notable_alcohol_problem("Тест checks детектив бета")
    qid = checks.detective_question_id(problem_id, "алкоголь", 0)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.resolve_question(cur, qid, "detective", "check", "Без алкоголя",
                                          problem_id=problem_id, factor="алкоголь", lag_days=0,
                                          window_days=21)
        conn.commit()
    assert result["created_rec_id"] is not None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT type, metric_key, direction, magnitude, window_days, freq_min_ratio FROM {t} "
                    "WHERE rec_id = %s").format(t=_t("expectation")),
            (result["created_rec_id"],),
        )
        ex_type, metric_key, direction, magnitude, window_days, freq_min_ratio = cur.fetchone()
        cur.execute(sql.SQL("SELECT 1 FROM {t} WHERE metric_key = %s").format(t=_t("metric_coverage")),
                    (metric_key,))
        covered = cur.fetchone() is not None
    assert ex_type == "frequency"
    assert metric_key == f"episode_count:{problem_id}"
    assert direction == "down" and float(magnitude) == 0
    assert window_days == 21
    assert covered, "metric_coverage должен знать про episode_count:<problem_id>, иначе G2 понизил бы до unmeasurable"


def test_checked_detective_question_disappears_from_questions_and_appears_in_checks():
    problem_id = _notable_alcohol_problem("Тест checks детектив гамма")
    qid = checks.detective_question_id(problem_id, "алкоголь", 0)
    with get_conn() as conn, conn.cursor() as cur:
        checks.resolve_question(cur, qid, "detective", "check", "Без алкоголя",
                                 problem_id=problem_id, factor="алкоголь", lag_days=0)
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur)
    assert qid not in [q["id"] for q in result["items"]["questions"]]
    assert any(c["source"] == "детектив" for c in result["items"]["checks"])


# ─────── проверки: recommendation+expectation активные ───────

def test_active_measurable_recommendation_shows_as_check_with_progress():
    rec_id = _active_recommendation(window_days=14, started_days_ago=4)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="checks")
    row = next(c for c in result["items"] if c["id"] == rec_id)
    assert row["status"] == "идёт"
    assert row["window_days"] == 14
    assert row["day_n"] == 5  # started 4 дня назад -> сегодня 5-й день окна
    assert row["day_of"] == 14
    assert row["verdict_date"] is not None


def test_recommendation_kind_consilium_maps_to_source():
    rec_id = _active_recommendation(kind="consilium", origin="consilium")
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="checks")
    row = next(c for c in result["items"] if c["id"] == rec_id)
    assert row["source"] == "консилиум"


def test_unmeasurable_recommendation_not_in_checks():
    from app.recommendations import RecommendationSyncRequest, sync_recommendation
    req = RecommendationSyncRequest(
        title="Неизмеримая рекомендация тест", source_ref=f"test-checks-unmeasurable:{ULID()}",
        started_ts=datetime.now(timezone.utc), unmeasurable_reason="тест",
    )
    sync_recommendation(req)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur, mode="checks")
    assert result["items"] == []


def test_effective_verdict_promotes_to_habit_not_check():
    rec_id = _active_recommendation()
    _set_current_verdict(rec_id, "effective")
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur)
    assert rec_id not in [c["id"] for c in result["items"]["checks"]]
    habit = next(h for h in result["items"]["habits"] if h["id"] == rec_id)
    assert habit["status"] == "привычка"


def test_partial_verdict_stays_a_check_not_a_habit():
    rec_id = _active_recommendation()
    _set_current_verdict(rec_id, "partial")
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.list_checks(cur)
    assert rec_id in [c["id"] for c in result["items"]["checks"]]
    assert result["items"]["habits"] == []


# ─────── главная: сводка + «Решить» ───────

def test_checks_summary_none_when_no_active_checks():
    with get_conn() as conn, conn.cursor() as cur:
        assert checks.checks_summary(cur) is None


def test_pending_questions_count_zero_when_none():
    with get_conn() as conn, conn.cursor() as cur:
        assert checks.pending_questions_count(cur) == 0


def test_pending_questions_count_includes_disagreements_not_in_home_inbox():
    """Живая жалоба Влада (2026-09-28): точка на вкладке «Проверки» должна
    гореть, даже когда home_inbox() пуст — разногласия консилиума туда не
    попадают вовсе (см. её докстринг), но это всё ещё нерешённый вопрос."""
    _write_disagreement()
    with get_conn() as conn, conn.cursor() as cur:
        assert checks.pending_questions_count(cur) == 1
        assert checks.home_inbox(cur) == []  # именно это Влад и поймал


def test_checks_summary_counts_and_nearest_date():
    # metric_key разный на каждый вызов — иначе оба попадут в один topic_key
    # (_derive_topic_key -> "metric_<metric_key>") и второй засуперседит первый
    # (app/recommendations.py::_supersede_same_topic — то же самое поведение,
    # что видел бы Влад на двух реальных проверках одной и той же метрики).
    _active_recommendation(metric_key="hrv", window_days=7, started_days_ago=1)
    _active_recommendation(metric_key="rhr", direction="down", window_days=30, started_days_ago=1)
    with get_conn() as conn, conn.cursor() as cur:
        summary = checks.checks_summary(cur)
    assert summary["count"] == 2
    assert summary["nearest_verdict_date"] is not None


def test_home_inbox_includes_recent_verdict():
    rec_id = _active_recommendation()
    _set_current_verdict(rec_id, "effective", ts_computed=datetime.now(timezone.utc) - timedelta(hours=2))
    with get_conn() as conn, conn.cursor() as cur:
        inbox = checks.home_inbox(cur)
    assert any(i["kind"] == "verdict" and i["ref"]["id"] == rec_id for i in inbox)


def test_home_inbox_excludes_stale_verdict():
    rec_id = _active_recommendation()
    _set_current_verdict(rec_id, "effective", ts_computed=datetime.now(timezone.utc) - timedelta(days=10))
    with get_conn() as conn, conn.cursor() as cur:
        inbox = checks.home_inbox(cur)
    assert not any(i["ref"].get("id") == rec_id for i in inbox if i["kind"] == "verdict")


def test_home_inbox_excludes_data_gap_verdict():
    rec_id = _active_recommendation()
    _set_current_verdict(rec_id, "data_gap")
    with get_conn() as conn, conn.cursor() as cur:
        inbox = checks.home_inbox(cur)
    assert not any(i["ref"].get("id") == rec_id for i in inbox if i["kind"] == "verdict")


def test_home_inbox_includes_fresh_detective_question():
    problem_id = _notable_alcohol_problem("Тест checks инбокс дельта")
    with get_conn() as conn, conn.cursor() as cur:
        inbox = checks.home_inbox(cur)
    assert any(i["kind"] == "question" and problem_id in i["id"] for i in inbox)


def test_home_inbox_excludes_detective_question_seen_over_3_days_ago():
    problem_id = _notable_alcohol_problem("Тест checks инбокс эпсилон")
    qid = checks.detective_question_id(problem_id, "алкоголь", 0)
    with get_conn() as conn, conn.cursor() as cur:
        checks.home_inbox(cur)  # первый вызов регистрирует first_seen_ts=now()
        cur.execute(
            sql.SQL("UPDATE {t} SET first_seen_ts = now() - interval '10 days' WHERE question_id = %s")
            .format(t=_t("vita_question_seen")),
            (qid,),
        )
        conn.commit()
        inbox = checks.home_inbox(cur)
    assert not any(i["id"] == qid for i in inbox)
    # но во вкладке «Вопросы» она продолжает жить (Часть 2.5: "дальше живёт в Вопросах")
    with get_conn() as conn, conn.cursor() as cur:
        questions = checks.list_checks(cur, mode="questions")
    assert any(q["id"] == qid for q in questions["items"])


# ─────── доказательства кейса (через агрегатор — метка гипотеза/факт) ───────

def test_case_evidence_view_unknown_problem_returns_none():
    with get_conn() as conn, conn.cursor() as cur:
        assert checks.case_evidence_view(cur, "pb_does_not_exist") is None


def test_case_evidence_view_marks_finding_as_hypothesis_before_any_decision():
    problem_id = _notable_alcohol_problem("Тест checks доказательства дзета")
    with get_conn() as conn, conn.cursor() as cur:
        view = checks.case_evidence_view(cur, problem_id)
    finding = next(f for f in view["findings"] if f["factor"] == "алкоголь")
    assert finding["evidence_grade"] == "гипотеза"


def test_case_evidence_view_marks_finding_as_fact_after_effective_verdict():
    problem_id = _notable_alcohol_problem("Тест checks доказательства эта")
    qid = checks.detective_question_id(problem_id, "алкоголь", 0)
    with get_conn() as conn, conn.cursor() as cur:
        result = checks.resolve_question(cur, qid, "detective", "check", "Без алкоголя",
                                          problem_id=problem_id, factor="алкоголь", lag_days=0)
        conn.commit()
    _set_current_verdict(result["created_rec_id"], "effective")
    with get_conn() as conn, conn.cursor() as cur:
        view = checks.case_evidence_view(cur, problem_id)
    finding = next(f for f in view["findings"] if f["factor"] == "алкоголь")
    assert finding["evidence_grade"] == "факт"


# ─────── фоновая синхронизация фактов частоты эпизодов ───────

def test_ensure_frequency_metric_coverage_idempotent():
    with get_conn() as conn, conn.cursor() as cur:
        k1 = checks.ensure_frequency_metric_coverage(cur, "pb_x")
        k2 = checks.ensure_frequency_metric_coverage(cur, "pb_x")
        conn.commit()
        cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE metric_key = %s").format(t=_t("metric_coverage")), (k1,))
        n = cur.fetchone()[0]
    assert k1 == k2 == "episode_count:pb_x"
    assert n == 1


def test_sync_writes_zero_count_on_clean_day_and_real_count_with_episodes():
    problem_id = _notable_alcohol_problem("Тест checks синк тета")
    qid = checks.detective_question_id(problem_id, "алкоголь", 0)
    with get_conn() as conn, conn.cursor() as cur:
        checks.resolve_question(cur, qid, "detective", "check", "Без алкоголя",
                                 problem_id=problem_id, factor="алкоголь", lag_days=0)
        conn.commit()
        n = checks.sync_episode_frequency_facts(cur)
        conn.commit()
        cur.execute(
            sql.SQL("SELECT value_num FROM {t} WHERE metric_key = %s ORDER BY ts_event DESC LIMIT 1")
            .format(t=_t("fact")),
            (f"episode_count:{problem_id}",),
        )
        value = cur.fetchone()[0]
    assert n == 1
    assert float(value) == 0.0  # эпизоды в фикстуре — 10-18 дней назад, не сегодня


def test_sync_is_a_noop_when_nothing_tracked():
    with get_conn() as conn, conn.cursor() as cur:
        assert checks.sync_episode_frequency_facts(cur) == 0


def test_question_headline_cuts_at_detail_marker():
    from app.checks import question_headline
    assert question_headline("Целевой уровень ЛПНП (<2.6 ммоль/л у кардиолога) — закрывается: х") == "Целевой уровень ЛПНП"
    assert question_headline("Первичный генез боли: гастроэнтеролог ведёт гипотезу") == "Первичный генез боли"


def test_question_headline_truncates_on_word_boundary():
    from app.checks import question_headline, _HEADLINE_MAX
    h = question_headline("Срок контрольной пересдачи витамина D на старте приёма препарата в высокой дозировке на долгий срок")
    assert h.endswith("…") and len(h) <= _HEADLINE_MAX + 1 and " " not in h[-2:]


def test_question_headline_empty_is_empty():
    from app.checks import question_headline
    assert question_headline("") == "" and question_headline(None) == ""
