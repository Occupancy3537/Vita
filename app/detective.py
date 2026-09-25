"""«Детектив» (2026-09-26, Часть 3) — направленный анализ гипотез по активным
проблемам: «дни эпизодов × факторы» против контрольных дней. НЕ слепой движок
корреляций (тот отключён осознанно, см. CLAUDE.md/dashboard.py::_DISABLED_CORRELATIONS
— "слепой перебор пар на малых данных = шум") — фактор попадает в проверку
ТОЛЬКО если на него указывает уже существующая гипотеза: health.investigations
(TEXT-реестр — Границы тикета явно запрещают переносить его в объектную модель
в этом заходе), card.anomaly_disposition.hypotheses (мост «аномалия -> действие»)
или дифдиагнозы консилиума (card.opinion/disagreement). Сопоставление гипотезы с
конкретной problem — по пересечению слов (problem не имеет FK на investigations/
opinion, роль намеренно та же MVP-словарная, что CANONICAL_ENTITIES/_TOPIC_PATTERNS
по всему проекту), не NLP.

Формат вывода — буквально по тексту тикета: "эпизоды совпадают с X в N из M
случаев против базовой частоты X", лаги 0-2 дня, БЕЗ p-значений и БЕЗ слова
"причина" — только совпадение, "где копать"."""
import logging
from datetime import date, timedelta
from statistics import median
from typing import Optional

from psycopg import sql

from app.db import schema

logger = logging.getLogger(__name__)

MIN_EPISODES = 5           # Часть 3.4: меньше — честное "мало данных", не считаем
MIN_LAG_SAMPLE = 3         # меньше дней с данными на конкретном лаге — тоже мало для вывода
LAGS_DAYS = (0, 1, 2)
NOTABLE_MARGIN = 0.25      # 25 п.п. над базовой частотой — порог "стоит показать", не любая рябь
ACWR_HIGH_THRESHOLD = 1.5  # тот же порог, что dashboard.py::get_health_dashboard (load_high fallback)

# «Живая находка» 2026-09-26: nutrient:Алкоголь — ДВА разных metric_key в
# card.fact для одной и той же величины (старый n8n-инсёрт писал "nutrient:
# Алкоголь, гр", текущий app/nutrition_reports.py — "nutrient:Алкоголь" с
# 2026-09-24). Не чиню здесь (не задача этого тикета — переименование задним
# числом само по себе риск), но анализ фактора обязан читать ОБА ключа, иначе
# "базовая частота" молча считается по обрубленной истории.
_FACTOR_RULES: list[dict] = [
    {"label": "алкоголь", "keywords": ["алкогол", "вин", "пив", "спиртн"],
     "metric_keys": ["nutrient:Алкоголь", "nutrient:Алкоголь, гр"], "direction": "nonzero"},
    {"label": "кофеин", "keywords": ["кофе", "кофеин", "эспрессо"],
     "metric_keys": ["nutrient:Кофеин"], "direction": "above_median"},
    {"label": "насыщенные жиры", "keywords": ["жирн", "джерки", "копчен", "вялен", "фастфуд", "сало"],
     "metric_keys": ["nutrient:Насыщенные жиры"], "direction": "above_median"},
    {"label": "добавленный сахар", "keywords": ["сахар", "сладк", "десерт"],
     "metric_keys": ["nutrient:Добавленный сахар"], "direction": "above_median"},
    {"label": "длительность сна", "keywords": ["недосып", "мало спал", "бессонниц", "не выспался", "плохо спал"],
     "metric_keys": ["sleep_min"], "direction": "below_median"},
    {"label": "глубокий сон", "keywords": ["глубокий сон", "фаза сна", "качество сна"],
     "metric_keys": ["deep_sleep_min"], "direction": "below_median"},
    {"label": "нагрузка (ACWR)", "keywords": ["перегруз", "acwr", "интенсивн трениров"],
     "metric_keys": ["acwr"], "direction": "above_threshold", "threshold": ACWR_HIGH_THRESHOLD},
    {"label": "малоподвижность", "keywords": ["малоподвижн", "мало двигал", "сидяч", "долгое сидение"],
     "metric_keys": ["steps"], "direction": "below_median"},
    {"label": "влажность в спальне", "keywords": ["влажност"],
     "metric_keys": ["humidity_avg_pct"], "direction": "above_median"},
    {"label": "пыль/PM2.5", "keywords": ["пыль", "pm2"],
     "metric_keys": ["pm25_avg"], "direction": "above_median"},
    {"label": "стресс", "keywords": ["стресс", "тревог", "нервн", "переживан"],
     "metric_keys": ["stress"], "direction": "above_median"},
]


def _word_hits(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k in low for k in keywords)


def classify_hypothesis_text(text: str) -> list[dict]:
    """Одна гипотеза может указывать сразу на несколько факторов ("жирная еда
    и стресс перед приступом") — возвращает все совпавшие правила, не первое."""
    return [rule for rule in _FACTOR_RULES if _word_hits(text, rule["keywords"])]


def _problem_keywords(cur, problem_id: str, title: str) -> set[str]:
    """База для сопоставления TEXT-источников (investigations/консилиум) с
    конкретной problem — у неё нет FK на них (Границы тикета: не переносим
    реестр гипотез в объектную модель в этом заходе)."""
    from app.doctor.tools import _symptom_food_words
    words = set(_symptom_food_words(title))
    cur.execute(
        sql.SQL("SELECT symptom_key, context FROM {t} WHERE problem_id = %s")
        .format(t=sql.Identifier(schema(), "episode")),
        (problem_id,),
    )
    for symptom_key, context in cur.fetchall():
        words |= set(_symptom_food_words(symptom_key.replace("-", " ").replace("_", " ")))
        if context:
            words |= set(_symptom_food_words(context))
    return words


def gather_hypotheses(cur, problem_id: str, title: str) -> list[dict]:
    """{"source", "text", "differentiator"} — из трёх источников, отфильтровано
    пересечением слов с этой problem (Часть 3.1: "investigations + гипотезы
    моста аномалий + дифдиагнозы консилиума")."""
    from app.doctor.tools import _symptom_food_words

    keywords = _problem_keywords(cur, problem_id, title)
    if not keywords:
        return []
    hyps: list[dict] = []

    cur.execute("SELECT inv_id, trigger, trigger_detail, hypothesis FROM health.investigations")
    for inv_id, trigger, detail, hyp in cur.fetchall():
        if not hyp:
            continue
        text = " ".join(filter(None, [trigger, detail, hyp]))
        if _symptom_food_words(text) & keywords:
            hyps.append({"source": f"investigation:{inv_id}", "text": hyp, "differentiator": None})

    cur.execute(
        sql.SQL("SELECT metric_label, hypotheses FROM {t} WHERE hypotheses IS NOT NULL")
        .format(t=sql.Identifier(schema(), "anomaly_disposition")),
    )
    for metric_label, hyp_list in cur.fetchall():
        for h in (hyp_list or []):
            text = h.get("hypothesis", "")
            if text and _symptom_food_words(text) & keywords:
                hyps.append({"source": f"anomaly:{metric_label}", "text": text,
                            "differentiator": h.get("differentiator")})

    cur.execute(
        sql.SQL("SELECT author, claim FROM {t} ORDER BY ts_recorded DESC LIMIT 100")
        .format(t=sql.Identifier(schema(), "opinion")),
    )
    for author, claim in cur.fetchall():
        if claim and _symptom_food_words(claim) & keywords:
            hyps.append({"source": f"consilium:{author}", "text": claim, "differentiator": None})

    cur.execute(
        sql.SQL("SELECT opinion_doctor, opinion_advisor, significance FROM {t} ORDER BY ts_recorded DESC LIMIT 50")
        .format(t=sql.Identifier(schema(), "disagreement")),
    )
    for side_a, side_b, significance in cur.fetchall():
        # significance уже несёт "о чём спор — закрывается: <тест>" целиком
        # (см. consilium.py::_write_disagreement) — отдельного поля "about" нет.
        text = " ".join(filter(None, [side_a, side_b, significance]))
        if text and _symptom_food_words(text) & keywords:
            hyps.append({"source": "consilium:disagreement", "text": text, "differentiator": significance})

    return hyps


def _classify_all(hyps: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in hyps:
        for rule in classify_hypothesis_text(h["text"]):
            entry = out.setdefault(rule["label"], {"rule": rule, "differentiators": [], "sources": []})
            if h.get("differentiator") and h["differentiator"] not in entry["differentiators"]:
                entry["differentiators"].append(h["differentiator"])
            entry["sources"].append(h["source"])
    return out


def _fetch_daily_values(cur, metric_keys: list[str]) -> dict[date, float]:
    cur.execute(
        sql.SQL("SELECT ts_event::date, avg(value_num) FROM {t} WHERE metric_key = ANY(%s) "
                "AND value_num IS NOT NULL GROUP BY 1")
        .format(t=sql.Identifier(schema(), "fact")),
        (metric_keys,),
    )
    return {d: float(v) for d, v in cur.fetchall()}


def _is_present(value: float, rule: dict, personal_median: Optional[float]) -> Optional[bool]:
    """None — направление требует персонального медианного порога, но данных
    для него нет (честная деградация "не считаем", не 0/1 наугад)."""
    direction = rule["direction"]
    if direction == "nonzero":
        return value > 0
    if direction == "above_threshold":
        return value > rule["threshold"]
    if personal_median is None:
        return None
    if direction == "above_median":
        return value > personal_median
    if direction == "below_median":
        return value < personal_median
    return None


def episode_days(cur, problem_id: str) -> list[date]:
    cur.execute(
        sql.SQL("SELECT DISTINCT onset_ts::date FROM {t} WHERE problem_id = %s "
                "AND onset_ts IS NOT NULL ORDER BY 1")
        .format(t=sql.Identifier(schema(), "episode")),
        (problem_id,),
    )
    return [r[0] for r in cur.fetchall()]


def analyze_problem(cur, problem_id: str, title: str) -> dict:
    """Главная точка входа Части 3. status: not_enough_data | no_hypotheses |
    no_signal (проверили — совпадений нет, тоже честный результат) | notable."""
    days = episode_days(cur, problem_id)
    if len(days) < MIN_EPISODES:
        return {"status": "not_enough_data", "episodes": len(days), "min_required": MIN_EPISODES, "findings": []}

    hyps = gather_hypotheses(cur, problem_id, title)
    factors = _classify_all(hyps)
    if not factors:
        return {"status": "no_hypotheses", "episodes": len(days), "findings": []}

    episode_date_set = set(days)
    findings = []
    for label, entry in factors.items():
        rule = entry["rule"]
        daily = _fetch_daily_values(cur, rule["metric_keys"])
        if not daily:
            continue
        personal_median = (
            median(daily.values()) if rule["direction"] in ("above_median", "below_median") else None
        )

        for lag in LAGS_DAYS:
            targets = [d - timedelta(days=lag) for d in days]
            n_present = m_effective = 0
            for t in targets:
                v = daily.get(t)
                if v is None:
                    continue
                present = _is_present(v, rule, personal_median)
                if present is None:
                    continue
                m_effective += 1
                n_present += int(present)
            if m_effective < MIN_LAG_SAMPLE:
                continue

            control_hits = control_total = 0
            for d, v in daily.items():
                if d in episode_date_set:
                    continue
                present = _is_present(v, rule, personal_median)
                if present is None:
                    continue
                control_total += 1
                control_hits += int(present)
            if control_total == 0:
                continue

            base_rate = control_hits / control_total
            rate = n_present / m_effective
            if rate - base_rate >= NOTABLE_MARGIN:
                findings.append({
                    "factor": label, "lag_days": lag, "n": n_present, "m": m_effective,
                    "rate": round(rate, 2), "base_rate": round(base_rate, 2),
                    "differentiators": entry["differentiators"], "sources": entry["sources"],
                })

    findings.sort(key=lambda f: f["rate"] - f["base_rate"], reverse=True)
    return {"status": "notable" if findings else "no_signal", "episodes": len(days), "findings": findings}


def format_finding_line(f: dict) -> str:
    lag_txt = "тот же день" if f["lag_days"] == 0 else f"лаг {f['lag_days']}д"
    return (f"эпизоды совпадают с «{f['factor']}» ({lag_txt}) в {f['n']} из {f['m']} случаев "
            f"(у него {f['rate']*100:.0f}%) против базовой частоты {f['base_rate']*100:.0f}%")


def suggest_research_topic(cur, problem_id: str, title: str) -> Optional[str]:
    """Часть 4 — ТОЛЬКО предложение, никогда не пишет в card.research_topic
    сама (Часть 4: "автоматически не добавлять"). Источник — «Перспективное»
    последних консилиумов, пересекающееся по словам с этой problem и ещё не
    покрытое активной темой в card.research_topic."""
    from app.doctor.tools import _symptom_food_words

    keywords = _problem_keywords(cur, problem_id, title)
    if not keywords:
        return None
    cur.execute(
        sql.SQL("SELECT topic, emerging FROM {t} WHERE status = 'completed' ORDER BY ts_recorded DESC LIMIT 5")
        .format(t=sql.Identifier(schema(), "consilium_report")),
    )
    reports = cur.fetchall()
    if not reports:
        return None
    cur.execute(
        sql.SQL("SELECT search_term, label FROM {t} WHERE active = true")
        .format(t=sql.Identifier(schema(), "research_topic")),
    )
    existing = " ".join(f"{t} {l}" for t, l in cur.fetchall() if t or l).lower()

    for report_topic, emerging in reports:
        for item in (emerging or []):
            method = item.get("method") or ""
            if not method or not (_symptom_food_words(method) & keywords):
                continue
            if method.lower() in existing:
                continue
            return (f"тема «{method}» (из консилиума «{report_topic}») пересекается с проблемой "
                    f"«{title}» — обсуди с Владом, добавлять ли в card.research_topic")
    return None


def build_weekly_block(cur) -> str:
    """Часть 3.3 — блок «Детектив» для недельного отчёта советника. Пусто (нет
    ни одной активной problem, или ни одной из них нечего сказать) -> "" —
    блока нет вовсе, не пустая шапка (буквальное требование тикета)."""
    cur.execute(
        sql.SQL("SELECT id, title FROM {t} WHERE status = 'active'")
        .format(t=sql.Identifier(schema(), "problem")),
    )
    problems = cur.fetchall()
    if not problems:
        return ""

    sections = []
    for problem_id, title in problems:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {t} WHERE problem_id = %s AND onset_ts >= now() - interval '7 days'")
            .format(t=sql.Identifier(schema(), "episode")),
            (problem_id,),
        )
        new_this_week = cur.fetchone()[0]
        analysis = analyze_problem(cur, problem_id, title)

        lines = []
        if new_this_week:
            lines.append(f"новых эпизодов за неделю: {new_this_week}")

        if analysis["status"] == "notable":
            for f in analysis["findings"][:3]:
                lines.append(format_finding_line(f))
            diffs = [d for f in analysis["findings"] for d in f["differentiators"]]
            if diffs:
                seen = list(dict.fromkeys(diffs))[:3]
                lines.append("что различит гипотезы: " + "; ".join(seen))
        elif not new_this_week:
            continue  # ничего нового и нечего показать по фактору — эту неделю про неё молчим
        elif analysis["status"] == "not_enough_data":
            lines.append(f"пока мало данных для направленного анализа ({analysis['episodes']}/{MIN_EPISODES} эпизодов)")
        elif analysis["status"] == "no_hypotheses":
            lines.append("гипотез для проверки пока нет (расследование/аномалии/консилиум ничего не предложили)")
        else:  # no_signal
            lines.append("проверено по имеющимся гипотезам — совпадений с факторами не найдено")

        try:
            suggestion = suggest_research_topic(cur, problem_id, title)
        except Exception:
            logger.exception("detective: suggest_research_topic упал для %s — пропущено", problem_id)
            suggestion = None
        if suggestion:
            lines.append(f"💡 {suggestion}")

        if lines:
            sections.append(f"«{title}»:\n" + "\n".join(f"  {l}" for l in lines))

    if not sections:
        return ""
    return "🕵️ Детектив:\n" + "\n\n".join(sections)
