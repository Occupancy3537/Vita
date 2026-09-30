"""app/lab_optimizer.py — движок оптимизации сдачи анализов (тикет
«оптимизатор сдачи анализов», 2026-09-26, Часть 2). card.* — та же ситуация,
что test_recommendations.py: пишем через schema() (card_test в тестах),
отдельная изоляция не нужна."""
import json
from datetime import date, datetime, timedelta, timezone

from app import lab_optimizer as opt
from app.db import get_conn, schema
from app.lab_catalog import LAB_CATALOG

TODAY = date(2026, 9, 26)


def _di(code, due, source_type="standing", source_id=None, reason="test", urgent=False):
    return opt.DueItem(code, due, source_type, source_id, reason, urgent)


# ─────────────────────────── _merge_due ───────────────────────────

def test_merge_due_earlier_date_wins_across_sources():
    standing = [_di("M035", date(2027, 1, 1), "standing")]
    rec = [_di("M035", date(2026, 10, 1), "recommendation")]
    merged = opt._merge_due(rec, standing, one_time_seen=set())
    assert merged["M035"].due_date == date(2026, 10, 1)
    assert merged["M035"].source_type == "recommendation"


def test_merge_due_same_date_prefers_higher_priority_source():
    standing = [_di("M035", date(2026, 10, 1), "standing")]
    manual = [_di("M035", date(2026, 10, 1), "manual")]
    merged = opt._merge_due(manual, standing, one_time_seen=set())
    assert merged["M035"].source_type == "manual"


def test_merge_due_skips_one_time_marker_with_existing_history():
    req = [_di("M079", date(2026, 10, 1), "recommendation")]  # Lp(a), одноразовый
    merged = opt._merge_due(req, one_time_seen={"M079"})
    assert "M079" not in merged


def test_merge_due_includes_one_time_marker_without_history():
    req = [_di("M079", date(2026, 10, 1), "recommendation")]
    merged = opt._merge_due(req, one_time_seen=set())
    assert "M079" in merged


# ─────────────────────────── _build_panels: группировка ───────────────────────────

def test_build_panels_groups_overdue_items_into_one_panel():
    """Всё уже просрочено — один общий забор, не по одному на каждый маркер
    (Часть 2.1/2.5)."""
    items = [_di("M004", date(2025, 1, 1)), _di("M017", date(2024, 6, 1)), _di("M008", date(2023, 1, 1))]
    panels, conflicts, beyond = opt._build_panels(items, TODAY, horizon_days=180,
                                                    max_per_draw=12, group_window_days=14)
    assert len(panels) == 1
    assert panels[0]["date"] == TODAY.isoformat()
    assert {m["code"] for m in panels[0]["markers"]} == {"M004", "M017", "M008"}


def test_build_panels_short_interval_marker_not_pulled_early():
    """Синтетика: если бы в каталоге был анализ с интервалом <90 дней — его
    НЕЛЬЗЯ тянуть раньше срока ради объединения (Часть 2.5, критерий явно
    привязан к интервалу ≥90). M009 (интервал 365, реальный маркер) стоит
    ДАЛЕКО в будущем — не должен утянуться в панель сегодняшних просрочек."""
    items = [_di("M004", date(2025, 1, 1)), _di("M009", TODAY + timedelta(days=200))]
    panels, _, _ = opt._build_panels(items, TODAY, horizon_days=365, max_per_draw=12, group_window_days=14)
    # M009 просрочки не касается вообще — окно anchor'а (просроченный M004,
    # today+14) не дотягивается до +200 дней
    codes_by_panel = [{m["code"] for m in p["markers"]} for p in panels]
    assert {"M004"} in codes_by_panel
    assert not any("M009" in c and "M004" in c for c in codes_by_panel)


def test_build_panels_respects_max_per_draw_cap_with_overflow_push():
    codes = [f"T{i:03d}" for i in range(15)]  # 15 одиночек вне связок (ОАК-связка считается за 1 единицу — см. test_bundles.py)
    items = [_di(c, TODAY - timedelta(days=1)) for c in codes]
    panels, _, _ = opt._build_panels(items, TODAY, horizon_days=365, max_per_draw=12, group_window_days=14)
    assert panels[0]["n_markers"] == 12
    assert sum(p["n_markers"] for p in panels) == 15
    # переполнение ушло не в тот же день, а с паузой (Часть 2.4 «панель+2»)
    overflow_panel = next(p for p in panels if p["n_markers"] == 3)
    assert overflow_panel["date"] > panels[0]["date"]
    assert (date.fromisoformat(overflow_panel["date"]) - date.fromisoformat(panels[0]["date"])).days >= 20


def test_build_panels_urgent_item_gets_own_panel_and_conflict_entry():
    items = [_di("M004", TODAY + timedelta(days=2), urgent=True, reason="врач сказал срочно"),
             _di("M017", TODAY + timedelta(days=3))]
    panels, conflicts, _ = opt._build_panels(items, TODAY, horizon_days=180, max_per_draw=12, group_window_days=14)
    urgent_panels = [p for p in panels if p["n_markers"] == 1 and p["markers"][0]["code"] == "M004"]
    assert len(urgent_panels) == 1
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == "M004"
    assert "срочн" in conflicts[0]["reason"].lower()


def test_build_panels_beyond_horizon_reported_not_silently_dropped_or_extended():
    codes = [f"M{i:03d}" for i in range(40, 40 + 30)]  # много просроченного -> много волн по 12
    items = [_di(c, TODAY - timedelta(days=1)) for c in codes]
    panels, _, beyond = opt._build_panels(items, TODAY, horizon_days=60, max_per_draw=12, group_window_days=14)
    assert sum(p["n_markers"] for p in panels) + len(beyond) == 30
    assert all(date.fromisoformat(p["date"]) <= TODAY + timedelta(days=60) for p in panels)
    if beyond:
        assert all("would_be_date" in b and "reason" in b for b in beyond)


def test_build_panels_export_text_lists_all_markers_and_fasting_note():
    items = [_di("M004", TODAY), _di("M035", TODAY)]  # M004 fasting=True, M035 fasting=False
    panels, _, _ = opt._build_panels(items, TODAY, horizon_days=180, max_per_draw=12, group_window_days=14)
    text = panels[0]["export_text"]
    assert "Креатинин" in text and "Витамин D" in text
    assert "натощак" in text.lower()


# ─────────────────────────── интеграция с БД ───────────────────────────

def _seed_lab_result(cur, code, ts_event):
    from ulid import ULID
    cur.execute(
        f"INSERT INTO {schema()}.lab_result (id, ts_event, provenance, marker_key, value_num) "
        "VALUES (%s, %s, '{}', %s, 1)",
        (f"lrt_{ULID()}", ts_event, code),
    )


def _seed_recommendation(cur, rec_id, title, started_ts, status="active"):
    cur.execute(
        f"INSERT INTO {schema()}.recommendation (id, ts_event, provenance, title, status, started_ts) "
        "VALUES (%s, now(), '{}', %s, %s, %s)",
        (rec_id, title, status, started_ts),
    )


def _seed_intervention(cur, iv_id, name, started_ts, status="active"):
    cur.execute(
        f"INSERT INTO {schema()}.intervention (id, ts_event, provenance, kind, name, status, started_ts) "
        "VALUES (%s, now(), '{}', 'supplement', %s, %s, %s)",
        (iv_id, name, status, started_ts),
    )


def test_history_reads_last_date_and_count():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_lab_result(cur, "M999TESTA", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_lab_result(cur, "M999TESTA", datetime(2026, 6, 1, tzinfo=timezone.utc))
        conn.commit()
        last_dates, counts = opt._history(cur)
    assert last_dates["M999TESTA"] == date(2026, 6, 1)
    assert counts["M999TESTA"] == 2


def test_requests_from_recommendations_text_scan():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rc_test_opt_1",
                              "Пересдать липидограмму через 90 дней", datetime(2026, 6, 1, tzinfo=timezone.utc))
        conn.commit()
        out = opt._requests_from_recommendations(cur, TODAY)
    codes = {it.code for it in out if it.source_id == "rc_test_opt_1"}
    assert codes == {"M009", "M010", "M011", "M012"}
    due = next(it.due_date for it in out if it.code == "M009" and it.source_id == "rc_test_opt_1")
    assert due == date(2026, 6, 1) + timedelta(days=90)


def test_requests_from_recommendations_no_time_phrase_is_skipped():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_recommendation(cur, "rc_test_opt_2", "Следить за витамином D",
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        conn.commit()
        out = opt._requests_from_recommendations(cur, TODAY)
    assert not any(it.source_id == "rc_test_opt_2" for it in out)


def test_requests_from_interventions_matches_vitamin_d_rule():
    with get_conn() as conn, conn.cursor() as cur:
        _seed_intervention(cur, "iv_test_opt_1", "Витамин D3 + K2", datetime(2026, 6, 1, tzinfo=timezone.utc))
        conn.commit()
        out = opt._requests_from_interventions(cur, TODAY)
    matches = [it for it in out if it.source_id == "iv_test_opt_1"]
    assert len(matches) == 1
    assert matches[0].code == "M035"
    assert matches[0].due_date == date(2026, 6, 1) + timedelta(days=90)


def test_requests_manual_roundtrip_and_mark_fulfilled():
    with get_conn() as conn, conn.cursor() as cur:
        from ulid import ULID
        req_id = f"lr_test_{ULID()}"
        cur.execute(
            f"INSERT INTO {schema()}.lab_request (id, marker_code, source_type, reason, due_date, status) "
            "VALUES (%s, 'M999TESTB', 'visit', 'тестовый ручной запрос', %s, 'open')",
            (req_id, date(2026, 10, 1)),
        )
        conn.commit()
        out = opt._requests_manual(cur)
        assert any(it.code == "M999TESTB" for it in out)

        n = opt.mark_fulfilled(cur, ["M999TESTB"])
        conn.commit()
        assert n == 1
        out2 = opt._requests_manual(cur)
        assert not any(it.code == "M999TESTB" for it in out2)


def test_generate_plan_is_deterministic_across_two_runs():
    """Приёмка тикета: одинаковый вход — побайтово одинаковый план. Заводим
    несколько источников (история + рекомендация + интервенция), чтобы план
    был нетривиальным (несколько панелей), не просто пустым."""
    with get_conn() as conn, conn.cursor() as cur:
        _seed_lab_result(cur, "M004", datetime(2024, 1, 1, tzinfo=timezone.utc))
        _seed_lab_result(cur, "M035", datetime(2019, 1, 1, tzinfo=timezone.utc))
        _seed_recommendation(cur, "rc_test_opt_det", "Пересдать липидограмму через 90 дней",
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        _seed_intervention(cur, "iv_test_opt_det", "Витамин D3 + K2", datetime(2026, 6, 1, tzinfo=timezone.utc))
        conn.commit()
        plan1 = opt.generate_plan(cur, today=TODAY)
        plan2 = opt.generate_plan(cur, today=TODAY)
    assert len(plan1["panels"]) >= 2
    assert json.dumps(plan1, sort_keys=True, ensure_ascii=False) == json.dumps(plan2, sort_keys=True, ensure_ascii=False)


def test_generate_plan_returns_expected_shape():
    with get_conn() as conn, conn.cursor() as cur:
        plan = opt.generate_plan(cur, today=TODAY)
    for key in ("generated_at", "horizon_end", "panels", "conflicts", "beyond_horizon", "n_markers_planned"):
        assert key in plan
    assert plan["generated_at"] == TODAY.isoformat()
    for panel in plan["panels"]:
        assert panel["n_markers"] == len(panel["markers"])
        assert panel["export_text"]
