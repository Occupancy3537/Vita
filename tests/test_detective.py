"""«Детектив» (2026-09-26, часть 3) — app/detective.py: направленный анализ
"дни эпизодов × факторы" против контрольных дней, ТОЛЬКО по гипотезам из
investigations/anomaly_disposition/консилиума — не слепой перебор. Читает
health.investigations буквальным SQL (без REGISTRAR_HEALTH_SCHEMA — тот же
приём, что consilium.py::_full_anamnesis) — _isolate_real_schema_writes нужен."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from ulid import ULID

from app import detective as det
from app import problem as pm
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")


def _make_problem(title: str = "Тестовая проблема детектива") -> str:
    with get_conn() as conn, conn.cursor() as cur:
        result = pm.create_problem(cur, title)
        conn.commit()
    return result["id"]


def _insert_episode(problem_id: str, onset_days_ago: int, symptom_key: str = "test-detective-key",
                    context: str = None):
    ep_id = f"ep_{ULID()}"
    onset = datetime.now(timezone.utc) - timedelta(days=onset_days_ago)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.episode (id, ts_event, provenance, symptom_key, onset_ts, status, problem_id, context) "
            "VALUES (%s, now(), '{}', %s, %s, 'open', %s, %s)",
            (ep_id, symptom_key, onset, problem_id, context),
        )
        conn.commit()
    return ep_id


def _insert_fact(metric_key: str, value: float, days_ago: int):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.fact (id, ts_event, provenance, verification, metric_key, value_num) "
            "VALUES (%s, %s, '{}', 'auto', %s, %s)",
            (f"f_{ULID()}", ts, metric_key, value),
        )
        conn.commit()


# ─────── classify_hypothesis_text ───────

def test_classify_finds_alcohol_keyword():
    rules = det.classify_hypothesis_text("вчера выпил бокал вина перед сном")
    assert any(r["label"] == "алкоголь" for r in rules)


def test_classify_finds_multiple_factors_in_one_hypothesis():
    rules = det.classify_hypothesis_text("много жирного и сильный стресс на работе")
    labels = {r["label"] for r in rules}
    assert "насыщенные жиры" in labels and "стресс" in labels


def test_classify_returns_empty_for_unrelated_text():
    assert det.classify_hypothesis_text("общий осмотр без особенностей") == []


# ─────── gather_hypotheses — реальные health.investigations/anomaly_disposition/консилиум ───────

def test_gather_hypotheses_matches_investigation_by_keyword_overlap():
    problem_id = _make_problem("Боль в левом подреберье после еды")
    _insert_episode(problem_id, 1, context="боль после жирной еды")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO health.investigations (inv_id, opened, status, trigger, hypothesis) "
            "VALUES ('test-inv-detective', current_date, 'report_ready', 'тест', "
            "'жирная еда провоцирует боль в подреберье')"
        )
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            hyps = det.gather_hypotheses(cur, problem_id, "Боль в левом подреберье после еды")
        assert any("жирная еда" in h["text"] for h in hyps)
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM health.investigations WHERE inv_id = 'test-inv-detective'")
            conn.commit()


def test_gather_hypotheses_matches_anomaly_disposition_hypothesis():
    problem_id = _make_problem("Стрессовая кардиалгия")
    _insert_episode(problem_id, 1, symptom_key="test-cardialgia", context="боль в груди на фоне стресса")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.anomaly_disposition (id, metric_key, metric_label, date, severity, hypotheses) "
            "VALUES (%s, 'test_metric', 'Стресс дневной', current_date, 'strong', %s)",
            (f"ad_{ULID()}", json.dumps([{"hypothesis": "кардиалгия связана со стрессом на работе",
                                          "differentiator": "выходной без стресса — боли нет"}])),
        )
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        hyps = det.gather_hypotheses(cur, problem_id, "Стрессовая кардиалгия")
    matched = [h for h in hyps if "стресс" in h["text"]]
    assert matched
    assert matched[0]["differentiator"] == "выходной без стресса — боли нет"


def test_gather_hypotheses_unrelated_investigation_not_matched():
    problem_id = _make_problem("Сухость кожи пальцев")
    _insert_episode(problem_id, 1, symptom_key="test-dryskin", context="кожа сохнет зимой")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO health.investigations (inv_id, opened, status, trigger, hypothesis) "
            "VALUES ('test-inv-unrelated', current_date, 'report_ready', 'тест', "
            "'изжога связана с рефлюксом после ужина')"
        )
        conn.commit()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            hyps = det.gather_hypotheses(cur, problem_id, "Сухость кожи пальцев")
        assert not any("рефлюкс" in h["text"] for h in hyps)
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM health.investigations WHERE inv_id = 'test-inv-unrelated'")
            conn.commit()


# ─────── analyze_problem — статусы ───────

def test_analyze_problem_not_enough_data_below_min_episodes():
    problem_id = _make_problem()
    for i in range(3):
        _insert_episode(problem_id, i)
    with get_conn() as conn, conn.cursor() as cur:
        result = det.analyze_problem(cur, problem_id, "Тестовая проблема детектива")
    assert result["status"] == "not_enough_data"
    assert result["episodes"] == 3


def test_analyze_problem_no_hypotheses_when_nothing_matches():
    # Слова нарочно не пересекаются ни с чем в реальных health.investigations
    # (те же 1-2 живые строки читает и проде, и тест — "гипотез"/"тема" были бы
    # самоссылочным совпадением: слово "гипотеза" встречается в тексте самих
    # гипотез почти всегда, ложный сигнал не про эту проблему).
    title = "Плюшевый жираф синяя пуговица кварц"
    problem_id = _make_problem(title)
    for i in range(6):
        _insert_episode(problem_id, i, symptom_key="test-plushzhiraf")
    with get_conn() as conn, conn.cursor() as cur:
        result = det.analyze_problem(cur, problem_id, title)
    assert result["status"] == "no_hypotheses"


def test_analyze_problem_notable_finds_alcohol_coincidence(monkeypatch):
    # Слова title/context нарочно избегают частых клинических связок ("после",
    # "на фоне", "тест") — они реально встречаются в живых health.investigations
    # (не изолированы по схеме, читаются буквально) и дают ложное пересечение
    # с этим источником гипотез, если использовать их в тестовом тексте.
    title = "Мигрень от спиртного проверка альфа"
    problem_id = _make_problem(title)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.anomaly_disposition (id, metric_key, metric_label, date, severity, hypotheses) "
            "VALUES (%s, 'test_metric_alpha', 'проверка альфа', current_date, 'strong', %s)",
            (f"ad_{ULID()}", json.dumps([{"hypothesis": "мигрень от спиртного проверка альфа",
                                          "differentiator": "трезвая неделя без мигрени"}])),
        )
        conn.commit()
    # 5 эпизодов, все в дни с алкоголем; 5 контрольных дней без него.
    episode_offsets = [10, 12, 14, 16, 18]
    for off in episode_offsets:
        _insert_episode(problem_id, off, symptom_key="test-alcohol-pain", context="мигрень от спиртного")
        _insert_fact("nutrient:Алкоголь", 20.0, off)
    for off in [11, 13, 15, 17, 19]:
        _insert_fact("nutrient:Алкоголь", 0.0, off)

    with get_conn() as conn, conn.cursor() as cur:
        result = det.analyze_problem(cur, problem_id, title)
    assert result["status"] == "notable"
    assert any(f["factor"] == "алкоголь" and f["lag_days"] == 0 for f in result["findings"])
    finding = next(f for f in result["findings"] if f["factor"] == "алкоголь" and f["lag_days"] == 0)
    assert finding["n"] == 5 and finding["m"] == 5
    assert finding["base_rate"] == 0.0


def test_analyze_problem_no_signal_when_hypothesis_does_not_pan_out():
    # Гипотеза про алкоголь заводится ЯВНО через anomaly_disposition (card_test,
    # изолировано и надёжно) — не через health.investigations (живая, не
    # изолированная таблица, полагаться на её текст в тесте на "нет сигнала"
    # было бы хрупко: реальная строка может не содержать нужного слова вовсе,
    # и тест прошёл бы по случайной причине "гипотез не нашлось", не по сути).
    title = "Зудящая сыпь редкий узор бета"
    problem_id = _make_problem(title)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema()}.anomaly_disposition (id, metric_key, metric_label, date, severity, hypotheses) "
            "VALUES (%s, 'test_metric_nosignal', 'тест', current_date, 'strong', %s)",
            (f"ad_{ULID()}", json.dumps([{"hypothesis": "сыпь связана с алкоголем узор бета",
                                          "differentiator": "трезвая неделя"}])),
        )
        conn.commit()

    episode_offsets = [10, 12, 14, 16, 18]
    for off in episode_offsets:
        _insert_episode(problem_id, off, symptom_key="test-nosignal", context="узор бета")
        _insert_fact("nutrient:Алкоголь", 0.0, off)  # алкоголя не было ни разу
    for off in [11, 13, 15, 17, 19]:
        _insert_fact("nutrient:Алкоголь", 20.0, off)  # а вот в контрольные дни — было

    with get_conn() as conn, conn.cursor() as cur:
        result = det.analyze_problem(cur, problem_id, title)
    assert result["status"] == "no_signal"


# ─────── format_finding_line ───────

def test_format_finding_line_zero_lag_says_same_day():
    line = det.format_finding_line({"factor": "алкоголь", "lag_days": 0, "n": 4, "m": 5,
                                    "rate": 0.8, "base_rate": 0.1})
    assert "тот же день" in line and "4 из 5" in line and "80%" in line and "10%" in line


def test_format_finding_line_nonzero_lag_says_lag():
    line = det.format_finding_line({"factor": "стресс", "lag_days": 2, "n": 3, "m": 5,
                                    "rate": 0.6, "base_rate": 0.2})
    assert "лаг 2д" in line


# ─────── build_weekly_block ───────

def test_build_weekly_block_empty_when_no_active_problems():
    with get_conn() as conn, conn.cursor() as cur:
        assert det.build_weekly_block(cur) == ""


def test_build_weekly_block_shows_new_episode_count():
    problem_id = _make_problem("Блок недели тест")
    _insert_episode(problem_id, 2, symptom_key="test-weekblock")
    with get_conn() as conn, conn.cursor() as cur:
        block = det.build_weekly_block(cur)
    assert "Детектив" in block
    assert "Блок недели тест" in block
    assert "новых эпизодов за неделю: 1" in block


def test_build_weekly_block_silent_for_stale_problem_with_nothing_new():
    problem_id = _make_problem("Молчаливая проблема тест")
    _insert_episode(problem_id, 40, symptom_key="test-silentweek")  # старый эпизод, не за последнюю неделю
    with get_conn() as conn, conn.cursor() as cur:
        block = det.build_weekly_block(cur)
    assert block == ""
