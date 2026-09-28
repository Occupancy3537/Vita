"""«Проверки» (Vita v2, этап 2, 2026-09-28) — единый реестр модели
«Вопрос → Проверка → Привычка». ТОЛЬКО агрегатор поверх уже существующих
сущностей (recommendation+expectation, card.intervention, card.disagreement,
detective.analyze_problem()) — ничего не мигрируется, не дублируется
(Границы тикета). Персистентность здесь только для того, чего в системе ещё
нет: решение по вопросу (card.vita_question_decision) и "когда впервые
увидели" детективную находку (card.vita_question_seen — у disagreement уже
есть свой ts_recorded, отдельный счётчик ей не нужен).

Источники (Часть 1.2 тикета), дословно:
  recommendation+expectation = проверка         -> _recommendation_checks
  intervention из кейса      = проверка         -> _intervention_checks
  действие консилиума «ждёт решения» = вопрос   -> _disagreement_questions
  verdict                    = вердикт          -> вплетён в _recommendation_checks/_habits
  подтвердившаяся привычка (эффективный вердикт
    на поведенческой метрике) = привычка        -> _habits

О "действии консилиума «ждёт решения»": в проде синтезатор консилиума
(app/consilium.py::run_consilium) уже проводит каждое действие через ворота
G7 и propose_recommendation() СИНХРОННО — оно либо сразу становится
рекомендацией, либо отклоняется; промежуточного "ждёт" состояния у actions
физически нет (и Границы тикета прямо запрещают это менять здесь — "только
чтение"). Единственное, что в консилиуме РЕАЛЬНО ждёт решения человека —
card.disagreement (status='raised', никто и никогда его не резолвит) — это и
есть вопрос "ждёт решения" в буквальном, не переносном смысле.

Про "intervention из кейса": на сегодня НИ ОДИН путь записи не создаёт
card.intervention со ссылкой на problem_id в provenance (таблица вообще не
имеет FK на problem) — источник реализован и готов агрегировать такие
строки, как только/если появится путь записи, но живьём вернёт пустой
список. Не изобретаю такой путь записи в этом тикете — не был явно заказан
с конкретными параметрами (только propose_recommendation(expectation_type=
frequency) назван буквально, см. resolve_question).
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg import sql
from ulid import ULID

from app import detective, run_log, timeutil
from app.db import get_conn, schema
from app.journal import write_journal
from app.recommendations import _STATUS_TEXT as _VERDICT_TEXT
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

DEFAULT_CHECK_WINDOW_DAYS = 21          # «Проверить 3 недели» — буквально из тикета
FREQUENCY_MIN_RATIO_EPISODE_FREE = 0.9   # ≤ ~10% дней окна со срывом — ещё "подтвердилось"
HOME_SLOT_DAYS = 3                       # Часть 2.5: открытие детектива живёт на главной 3 дня
VERDICT_READY_DAYS = 3                   # симметрично — «вердикт готов» тоже 3 дня в «Решить»
SYNC_INTERVAL_SECONDS = 1800


def _t(name: str):
    return sql.Identifier(schema(), name)


# =====================================================================
# Вопросы (id детерминированный — детективные findings не персистентны,
# analyze_problem() пересчитывает их заново при каждом вызове)
# =====================================================================

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"


def detective_question_id(problem_id: str, factor: str, lag_days: int) -> str:
    return f"dq_{problem_id}_{_slug(factor)}_{lag_days}"


def disagreement_question_id(dis_id: str) -> str:
    return f"cq_{dis_id}"


def get_decisions(cur) -> dict[str, dict]:
    cur.execute(
        sql.SQL("SELECT question_id, source, decision, reason, created_rec_id, ts_recorded FROM {t}")
        .format(t=_t("vita_question_decision")),
    )
    return {
        qid: {"source": src, "decision": dec, "reason": reason, "created_rec_id": rec_id,
              "ts_recorded": ts.isoformat()}
        for qid, src, dec, reason, rec_id, ts in cur.fetchall()
    }


def record_question_decision(cur, question_id: str, source: str, decision: str,
                              reason: Optional[str] = None, created_rec_id: Optional[str] = None) -> None:
    cur.execute(
        sql.SQL("INSERT INTO {t} (question_id, source, decision, reason, created_rec_id) VALUES (%s, %s, %s, %s, %s)")
        .format(t=_t("vita_question_decision")),
        (question_id, source, decision, reason, created_rec_id),
    )
    write_journal(cur, "vita_question_decision", question_id, "create",
                  diff={"source": source, "decision": decision, "reason": reason, "created_rec_id": created_rec_id})


def _mark_seen(cur, question_id: str) -> datetime:
    cur.execute(
        sql.SQL("INSERT INTO {t} (question_id) VALUES (%s) ON CONFLICT (question_id) DO NOTHING")
        .format(t=_t("vita_question_seen")),
        (question_id,),
    )
    cur.execute(
        sql.SQL("SELECT first_seen_ts FROM {t} WHERE question_id = %s").format(t=_t("vita_question_seen")),
        (question_id,),
    )
    return cur.fetchone()[0]


def _active_problems(cur) -> list[tuple]:
    cur.execute(sql.SQL("SELECT id, title FROM {t} WHERE status = 'active'").format(t=_t("problem")))
    return cur.fetchall()


def _lag_label(lag_days: int) -> str:
    return "тот же день" if lag_days == 0 else f"лаг {lag_days}д"


def _detective_questions(cur, decisions: dict) -> list[dict]:
    out = []
    for problem_id, title in _active_problems(cur):
        try:
            analysis = detective.analyze_problem(cur, problem_id, title)
        except Exception:
            logger.exception("checks: analyze_problem упал для %s — пропущено", problem_id)
            continue
        if analysis["status"] != "notable":
            continue
        for f in analysis["findings"]:
            qid = detective_question_id(problem_id, f["factor"], f["lag_days"])
            if qid in decisions:
                continue  # уже решено — вопросом больше не считается
            first_seen = _mark_seen(cur, qid)
            out.append({
                "id": qid, "type": "вопрос", "source": "детектив",
                "title": f"{title}: {f['factor']}",
                "what": f"Совпадение с «{f['factor']}» ({_lag_label(f['lag_days'])})",
                "detail": f"{f['n']} из {f['m']} случаев ({f['rate']*100:.0f}%) против "
                          f"базовой частоты {f['base_rate']*100:.0f}%",
                "status": "ждёт решения",
                "problem_id": problem_id, "factor": f["factor"], "lag_days": f["lag_days"],
                "first_seen_ts": first_seen.isoformat(),
                "evidence_grade": "гипотеза",
            })
    return out


def _disagreement_questions(cur, decisions: dict) -> list[dict]:
    cur.execute(
        sql.SQL("SELECT id, opinion_doctor, opinion_advisor, significance, ts_recorded, report_id FROM {t} "
                "WHERE status = 'raised' ORDER BY ts_recorded DESC").format(t=_t("disagreement")),
    )
    out = []
    for dis_id, a, b, significance, ts, report_id in cur.fetchall():
        qid = disagreement_question_id(dis_id)
        if qid in decisions:
            continue
        between = " ↔ ".join(x for x in (a, b) if x)
        out.append({
            "id": qid, "type": "вопрос", "source": "консилиум",
            "title": between or "Разногласие консилиума",
            "what": significance or "",
            "detail": significance or "",
            "status": "ждёт решения",
            "disagreement_id": dis_id, "report_id": report_id,
            "first_seen_ts": ts.isoformat(),
            "evidence_grade": "гипотеза",
        })
    return out


# =====================================================================
# Проверки (recommendation+expectation активные измеримые, + intervention-
# из-кейса как будущий источник — см. докстринг модуля)
# =====================================================================

def _expectation_text(label: Optional[str], direction: Optional[str], magnitude, unit: Optional[str]) -> str:
    if magnitude is None or not label:
        return label or ""
    arrow = "↑" if direction == "up" else "↓" if direction == "down" else ""
    return f"{label} {arrow}{magnitude:g}{unit or ''}"


def _source_from(kind: Optional[str], provenance: Optional[dict]) -> str:
    provenance = provenance or {}
    origin = provenance.get("origin")
    source_ref = str(provenance.get("source_ref") or "")
    if kind == "consilium" or origin == "consilium":
        return "консилиум"
    if source_ref.startswith("vita_question:dq_") or origin == "detective":
        return "детектив"
    if origin == "advisor":
        return "врач"
    return "врач"


def _recommendation_rows(cur):
    cur.execute(
        sql.SQL(
            "SELECT rc.id, rc.title, rc.kind, rc.provenance, rc.started_ts, "
            "ex.metric_key, ex.metric_label, ex.direction, ex.magnitude, ex.unit, ex.window_days, ex.lag_days, "
            "rv.verdict, rv.ts_computed, rv.id AS verdict_id "
            "FROM {rc} rc JOIN {ex} ex ON ex.rec_id = rc.id AND ex.role = 'primary' AND ex.cycle = rc.cycle "
            "LEFT JOIN {rv} rv ON rv.rec_id = rc.id AND rv.status = 'current' "
            "WHERE rc.status = 'active' AND ex.type != 'unmeasurable' AND rc.started_ts IS NOT NULL "
            "ORDER BY rc.started_ts DESC"
        ).format(rc=_t("recommendation"), ex=_t("expectation"), rv=_t("recommendation_verdict")),
    )
    return cur.fetchall()


def _recommendation_checks(cur) -> list[dict]:
    """'идёт' — активная измеримая рекомендация БЕЗ текущего эффективного
    вердикта (эффективный -> _habits, не дублируем строку)."""
    now = timeutil.now_local()
    out = []
    for (rec_id, title, kind, provenance, started_ts, metric_key, metric_label, direction, magnitude,
         unit, window_days, lag_days, verdict, ts_computed, verdict_id) in _recommendation_rows(cur):
        if verdict == "effective":
            continue
        window_start = started_ts + timedelta(days=lag_days or 0)
        day_n = day_of = verdict_date = None
        if window_days:
            day_of = window_days
            day_n = max(0, min(window_days, (now - window_start).days + 1))
            verdict_date = (window_start + timedelta(days=window_days)).date().isoformat()
        out.append({
            "id": rec_id, "type": "проверка", "source": _source_from(kind, provenance),
            "title": title, "what": title,
            "expectation": _expectation_text(metric_label or metric_key, direction, magnitude, unit),
            "window_days": window_days, "day_n": day_n, "day_of": day_of,
            "status": "идёт" if verdict is None else "вердикт",
            "latest_verdict": verdict, "verdict_label": _VERDICT_TEXT.get(verdict) if verdict else None,
            "verdict_date": verdict_date,
            "ref": {"type": "recommendation", "id": rec_id},
        })
    return out


def _intervention_checks(cur) -> list[dict]:
    """Источник готов, но сегодня пуст — см. докстринг модуля."""
    cur.execute(
        sql.SQL("SELECT id, name, provenance, started_ts FROM {t} "
                "WHERE status = 'active' AND provenance ? 'problem_id'").format(t=_t("intervention")),
    )
    out = []
    for iv_id, name, provenance, started_ts in cur.fetchall():
        out.append({
            "id": iv_id, "type": "проверка", "source": "кейс",
            "title": name, "what": name, "expectation": None,
            "window_days": None, "day_n": None, "day_of": None,
            "status": "идёт", "latest_verdict": None, "verdict_label": None, "verdict_date": None,
            "ref": {"type": "intervention", "id": iv_id, "problem_id": (provenance or {}).get("problem_id")},
        })
    return out


def _habits(cur) -> list[dict]:
    """'подтвердившаяся привычка (эффективный вердикт на поведенческой
    метрике) = привычка' — буквально verdict == 'effective' (partial НЕ
    считается — тикет говорит "эффективный", partial остаётся в «Идут»)."""
    out = []
    for (rec_id, title, kind, provenance, started_ts, metric_key, metric_label, direction, magnitude,
         unit, window_days, lag_days, verdict, ts_computed, verdict_id) in _recommendation_rows(cur):
        if verdict != "effective":
            continue
        out.append({
            "id": rec_id, "type": "привычка", "source": _source_from(kind, provenance),
            "title": title,
            "expectation": _expectation_text(metric_label or metric_key, direction, magnitude, unit),
            "status": "привычка",
            "verdict_date": ts_computed.date().isoformat() if ts_computed else None,
            "ref": {"type": "recommendation_verdict", "id": verdict_id},
        })
    return out


# =====================================================================
# Агрегатор
# =====================================================================

_MODES = ("questions", "checks", "habits")


def list_checks(cur, mode: Optional[str] = None) -> dict:
    decisions = get_decisions(cur)
    items = {
        "questions": _detective_questions(cur, decisions) + _disagreement_questions(cur, decisions),
        "checks": _recommendation_checks(cur) + _intervention_checks(cur),
        "habits": _habits(cur),
    }
    counts = {k: len(v) for k, v in items.items()}
    if mode is not None:
        if mode not in _MODES:
            raise ValueError(f"неизвестный режим: {mode}")
        return {"mode": mode, "items": items[mode], "counts": counts}
    return {"items": items, "counts": counts}


def checks_summary(cur) -> Optional[dict]:
    """Часть 3.1 — «N проверок идут · ближайший вердикт <дата>» на главной."""
    checks = _recommendation_checks(cur) + _intervention_checks(cur)
    if not checks:
        return None
    dates = [c["verdict_date"] for c in checks if c.get("verdict_date")]
    return {"count": len(checks), "nearest_verdict_date": min(dates) if dates else None}


def pending_questions_count(cur) -> int:
    """Живая жалоба Влада (2026-09-28): «если на "Проверках" требуется моё
    действие — там должна стоять точка» — home_inbox() специально показывает
    в слоте «Решить» только СВЕЖИЕ события (вердикт/детектив за 3 дня), но
    точка на вкладке должна гореть, пока ЕСТЬ хоть один нерешённый вопрос
    (включая разногласия консилиума, которые в слот «Решить» никогда не
    попадают, см. докстринг home_inbox) — отдельный, полный счётчик."""
    decisions = get_decisions(cur)
    return len(_detective_questions(cur, decisions)) + len(_disagreement_questions(cur, decisions))


def home_inbox(cur) -> list[dict]:
    """Часть 3.2 — слот «Решить» на главной (этап 1 оставил его пустым):
    вердикт готов (последние VERDICT_READY_DAYS дней) + открытие детектива
    (первые HOME_SLOT_DAYS дней с first_seen, дальше живёт только в «Вопросы»)."""
    now = timeutil.now_local()
    out = []
    cur.execute(
        sql.SQL(
            "SELECT rv.id, rv.rec_id, rv.verdict, rv.ts_computed, rc.title FROM {rv} rv "
            "JOIN {rc} rc ON rc.id = rv.rec_id "
            "WHERE rv.status = 'current' AND rv.verdict != 'data_gap' AND rv.ts_computed >= %s "
            "ORDER BY rv.ts_computed DESC"
        ).format(rv=_t("recommendation_verdict"), rc=_t("recommendation")),
        (now - timedelta(days=VERDICT_READY_DAYS),),
    )
    for verdict_id, rec_id, verdict, ts_computed, title in cur.fetchall():
        out.append({
            "id": f"verdict:{verdict_id}", "kind": "verdict",
            "tone": "green" if verdict in ("effective", "partial") else "amber",
            "title": f"Вердикт готов: {title}",
            "sub": _VERDICT_TEXT.get(verdict, verdict),
            "ref": {"type": "recommendation", "id": rec_id},
        })

    decisions = get_decisions(cur)
    for q in _detective_questions(cur, decisions):
        seen = datetime.fromisoformat(q["first_seen_ts"])
        if now - seen <= timedelta(days=HOME_SLOT_DAYS):
            out.append({
                "id": q["id"], "kind": "question", "tone": "amber",
                "title": q["title"], "sub": q["detail"],
                "ref": {"type": "detective", "id": q["id"]},
            })
    return out


# =====================================================================
# Доказательства кейса (обёртка над detective.case_evidence — добавляет
# метку гипотеза/факт, которую detective.py намеренно не знает, см. её
# докстринг: он ничего не знает про card.vita_question_decision)
# =====================================================================

def _grade_for_finding(cur, decision: Optional[dict]) -> str:
    if not decision or decision["decision"] != "checked" or not decision.get("created_rec_id"):
        return "гипотеза"
    cur.execute(
        sql.SQL("SELECT verdict FROM {t} WHERE rec_id = %s AND status = 'current'")
        .format(t=_t("recommendation_verdict")),
        (decision["created_rec_id"],),
    )
    row = cur.fetchone()
    return "факт" if row and row[0] in ("effective", "partial") else "гипотеза"


def case_evidence_view(cur, problem_id: str) -> Optional[dict]:
    cur.execute(sql.SQL("SELECT title FROM {t} WHERE id = %s").format(t=_t("problem")), (problem_id,))
    row = cur.fetchone()
    if row is None:
        return None
    result = detective.case_evidence(cur, problem_id, row[0])
    decisions = get_decisions(cur)
    for f in result["findings"]:
        qid = detective_question_id(problem_id, f["factor"], f["lag_days"])
        f["evidence_grade"] = _grade_for_finding(cur, decisions.get(qid))
    return result


# =====================================================================
# Решение по вопросу (Часть 2.3) — «Проверить N недель» / «Отклонить»
# =====================================================================

def ensure_frequency_metric_coverage(cur, problem_id: str) -> str:
    """Регистрирует episode_count:<problem_id> в card.metric_coverage —
    без этого G2 (gates.py) молча понизил бы frequency-ожидание до
    unmeasurable (metric_key не в реестре = "неизмеримо технически"),
    хотя тикет буквально просит именно frequency. Пишет через переданный
    cur БЕЗ commit — см. resolve_question, где перед вызовом
    propose_recommendation() (СВОЁ отдельное соединение) нужен отдельный,
    уже закоммиченный вызов через _ensure_frequency_metric_coverage_committed —
    иначе propose_recommendation с другого соединения эту запись ещё не видит
    (живой баг, пойманный на реальном API: рекомендация тихо становилась
    unmeasurable несмотря на то, что метрика "уже" зарегистрирована — просто
    не в той транзакции)."""
    metric_key = f"episode_count:{problem_id}"
    cur.execute(
        sql.SQL("INSERT INTO {t} (metric_key, observer, frequency) VALUES (%s, 'card', 'daily') "
                "ON CONFLICT (metric_key) DO NOTHING").format(t=_t("metric_coverage")),
        (metric_key,),
    )
    return metric_key


def _ensure_frequency_metric_coverage_committed(problem_id: str) -> str:
    """Та же регистрация, но в СВОЁМ соединении с явным commit — нужна ДО
    propose_recommendation() (у неё своё соединение, не видит незакоммиченные
    записи из курсора resolve_question)."""
    with get_conn() as conn, conn.cursor() as cur:
        metric_key = ensure_frequency_metric_coverage(cur, problem_id)
        conn.commit()
    return metric_key


def resolve_question(cur, question_id: str, source: str, action: str, title: str,
                      reason: Optional[str] = None, window_days: Optional[int] = None,
                      problem_id: Optional[str] = None, factor: Optional[str] = None,
                      lag_days: Optional[int] = None) -> dict:
    """Часть 2.3: «Проверить N недель» -> propose_recommendation(
    expectation_type=frequency) для детективных вопросов (буквально по
    тексту тикета — трекает частоту эпизодов, ждём ~0 в окне). Для вопроса
    консилиума (disagreement) частота эпизодов не подходит по смыслу
    (resolving_test обычно разовый — «сделать УЗИ», не поведенческий тест
    3 недели) — пишем рекомендацию честно unmeasurable с причиной, тот же
    принцип G7 "проверка есть всегда, просто не всегда числовая".
    «Отклонить» — decision='declined', ничего не создаёт, вопрос просто
    перестаёт быть вопросом (get_decisions фильтрует его на будущее).

    Вызывает propose_recommendation() — своя ОТДЕЛЬНАЯ транзакция (см. её
    докстринг), не курсор этой функции; caller коммитит после возврата.
    propose_recommendation дедуплицирует по source_ref (ON CONFLICT DO
    NOTHING) — повторный вызов при сбое между созданием рекомендации и
    записью решения безопасен, не создаст вторую."""
    if action not in ("check", "decline"):
        raise ValueError(f"неизвестное действие: {action}")
    decisions = get_decisions(cur)
    if question_id in decisions:
        return {"question_id": question_id, "already_decided": True, **decisions[question_id]}

    created_rec_id = None
    if action == "check":
        from app.recommendations import ProposeRequest, propose_recommendation

        if source == "detective":
            if not problem_id:
                raise ValueError("problem_id обязателен для проверки детективного вопроса")
            metric_key = _ensure_frequency_metric_coverage_committed(problem_id)
            resp = propose_recommendation(ProposeRequest(
                title=f"Проверка: {title}", action=title,
                rationale=f"открытие детектива — {factor or 'см. вопрос'}"
                          + (f", лаг {lag_days}д" if lag_days is not None else ""),
                kind="detective_check", source_ref=f"vita_question:{question_id}", origin="detective",
                started_ts=datetime.now(timezone.utc),
                metric_key=metric_key, metric_label="Эпизоды", unit=" эп/день",
                direction="down", magnitude=0,
                window_days=window_days or DEFAULT_CHECK_WINDOW_DAYS,
                expectation_type="frequency", freq_min_ratio=FREQUENCY_MIN_RATIO_EPISODE_FREE,
                is_bioage_driver=False, metric_overdue=False,
            ))
        elif source == "disagreement":
            resp = propose_recommendation(ProposeRequest(
                title=title, action=title,
                rationale=reason or "разногласие консилиума — проверка по значимому наблюдению",
                kind="consilium_check", source_ref=f"vita_question:{question_id}", origin="consilium",
                started_ts=datetime.now(timezone.utc),
                unmeasurable_reason="разногласие консилиума — проверяется тестом/наблюдением, не частотой",
                is_bioage_driver=False, metric_overdue=False,
            ))
        else:
            raise ValueError(f"неизвестный источник вопроса: {source}")
        if resp.accepted:
            created_rec_id = resp.id

    record_question_decision(cur, question_id, source, "checked" if action == "check" else "declined",
                              reason=reason, created_rec_id=created_rec_id)
    return {"question_id": question_id, "decision": action, "created_rec_id": created_rec_id}


# =====================================================================
# Фоновая синхронизация: card.fact(episode_count:<problem_id>) — daily,
# нужно verdict_engine::_evaluate_frequency (Часть "Приёмка": «Проверить
# 3 недели» -> «проверка с frequency-ожиданием», которая когда-нибудь
# реально досчитается до вердикта). 0 эпизодов пишем явно ЯВНО (не молчим) —
# иначе "чистый" день без эпизодов не попал бы в eval-окно вовсе (покрытие
# там считается по наличию факта за день, не по значению).
# =====================================================================

def _tracked_episode_problem_ids(cur) -> list[str]:
    cur.execute(
        sql.SQL(
            "SELECT DISTINCT ex.metric_key FROM {ex} ex JOIN {rc} rc ON rc.id = ex.rec_id "
            "WHERE rc.status = 'active' AND ex.role = 'primary' AND ex.type = 'frequency' "
            "AND ex.metric_key LIKE 'episode_count:%'"
        ).format(ex=_t("expectation"), rc=_t("recommendation")),
    )
    return [row[0].split(":", 1)[1] for row in cur.fetchall()]


def sync_episode_frequency_facts(cur) -> int:
    day = timeutil.today()
    tz = timeutil.person_tz()
    local_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start, utc_end = local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
    ts_event = f"{day.isoformat()}T00:00:00Z"  # тот же приём "день-маркер", что biohacking_ingest.py

    n = 0
    for problem_id in _tracked_episode_problem_ids(cur):
        metric_key = f"episode_count:{problem_id}"
        cur.execute(
            sql.SQL("SELECT count(*) FROM {t} WHERE problem_id = %s AND onset_ts >= %s AND onset_ts < %s")
            .format(t=_t("episode")),
            (problem_id, utc_start, utc_end),
        )
        count = cur.fetchone()[0]
        cur.execute(
            sql.SQL("DELETE FROM {t} WHERE metric_key = %s AND ts_event = %s").format(t=_t("fact")),
            (metric_key, ts_event),
        )
        fact_id = f"f_{ULID()}"
        cur.execute(
            sql.SQL("INSERT INTO {t} (id, ts_event, provenance, verification, metric_key, value_num) "
                    "VALUES (%s, %s, %s, 'auto', %s, %s)").format(t=_t("fact")),
            (fact_id, ts_event, json.dumps({"origin": "checks_episode_frequency_sync"}), metric_key, count),
        )
        n += 1
    return n


def run_once() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        n = sync_episode_frequency_facts(cur)
        conn.commit()
    if n:
        logger.info("checks: обновлены дневные факты частоты эпизодов для %d проверок", n)


def run_scheduler() -> None:
    logger.info("checks episode-frequency scheduler: старт (каждые %d мин)", SYNC_INTERVAL_SECONDS // 60)
    while True:
        try:
            run_once()
            run_log.mark_run("checks_episode_frequency")
        except Exception as e:
            logger.exception("checks: run_once упал целиком — повтор через обычный интервал")
            alert_on_failure("checks_episode_frequency", e)
        time.sleep(SYNC_INTERVAL_SECONDS)
