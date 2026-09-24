"""Write-path на моках LLM-извлечения (быстро, детерминированно, без реального
API-вызова) — реальный API проверяется отдельно в test_extraction_live.py."""
from unittest.mock import patch

from app.db import get_conn, schema
from app.extraction import Draft, ExtractionResult
from app.write_path import check_bracelet_intersection, process


def _mock_extract(drafts=None, no_medical=False):
    return ExtractionResult(no_medical_content=no_medical, drafts=drafts or [])


def _ingest(raw_text: str) -> str:
    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    r = client.post("/ingest", json={"channel": "telegram", "raw_text": raw_text})
    return r.json()["id"]


def test_bracelet_intersection_detects_known_allergen():
    assert check_bracelet_intersection("врач предложил новокаин для укола") == ["allergy:novocaine_anaphylaxis"]
    assert check_bracelet_intersection("съел минтай на обед") == ["allergy:pollock_fish"]
    assert check_bracelet_intersection("обычный день, всё хорошо") == []


def test_bracelet_intersection_negation_does_not_trigger():
    """Живой инцидент 2026-09-23: Влад процитировал старую рекомендацию доктора
    («Добавь омега-3 ... но не минтай — аллергия») с вопросом про питание —
    голое substring-совпадение слова «минтай» дало bracelet_hits и мгновенный
    L3-эмердженси вместо ответа на нормальный вопрос."""
    text = ('ты рекомендовал - "6. Питание против воспаления\n'
            'У тебя в карте активная рекомендация «насыщенные жиры ≤28 г/день» — '
            'продолжай. Добавь омега-3 (жирная рыба 2-3 р/нед, но не минтай — '
            'аллергия)." - я поискал, резорбция мне нужна, это и есть уменьшение '
            'грыжи, основа резорбции - это воспаление. Насколько правильный совет '
            'ты даешь?')
    assert check_bracelet_intersection(text) == []
    assert check_bracelet_intersection("нет, новокаин мне не давали") == []
    assert check_bracelet_intersection("без новокаина в этот раз") == []


def test_process_creates_new_episode():
    src_id = _ingest("болит голова с утра")
    with patch("app.write_path.extract", return_value=_mock_extract([
        Draft(symptom_key="headache", onset_expr="с утра", intensity=4, confidence=0.8)
    ])):
        result = process(src_id)

    assert result["written"][0]["action"] == "created_episode"
    ep_id = result["written"][0]["episode_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT symptom_key, status FROM {schema()}.episode WHERE id = %s", (ep_id,))
        assert cur.fetchone() == ("headache", "open")


def test_process_updates_existing_open_episode_within_48h():
    src1 = _ingest("болит голова с утра")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache")])):
        r1 = process(src1)
    ep_id = r1["written"][0]["episode_id"]

    src2 = _ingest("голова всё ещё болит")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache")])):
        r2 = process(src2)

    assert r2["written"][0]["action"] == "updated_episode"
    assert r2["written"][0]["episode_id"] == ep_id
    # П2 edge-кейс: "болит -> всё ещё болит" = 1 эпизод, 2 факта.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.episode WHERE symptom_key = 'headache'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.fact WHERE episode_id = %s", (ep_id,))
        assert cur.fetchone()[0] == 2


def test_process_closes_episode_on_negation():
    src1 = _ingest("болит голова")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache")])):
        r1 = process(src1)
    ep_id = r1["written"][0]["episode_id"]

    src2 = _ingest("голова уже прошла")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache", negation=True)])):
        r2 = process(src2)

    assert r2["written"][0]["action"] == "closed_episode"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, closure_source FROM {schema()}.episode WHERE id = %s", (ep_id,))
        assert cur.fetchone() == ("resolved", "user")


def test_process_flags_red_flag_independent_of_llm(monkeypatch):
    """Слой A срабатывает по сырому тексту ДО извлечения — даже если LLM решит,
    что содержания нет, красный флаг не теряется. F2 (2026-09-22): алерт
    владельцу мокаем — юнит-тест не должен слать реальный Telegram."""
    import app.write_path as wp
    monkeypatch.setattr(wp.notify, "notify", lambda *a, **k: None)

    src_id = _ingest("грудь давит, отдаёт в левую руку, одышка")
    with patch("app.write_path.extract", return_value=_mock_extract(no_medical=True)):
        result = process(src_id)
    assert result["flags"]["red_flag"]["hit"] is True
    assert any("КРАСНЫЙ ФЛАГ" in q for q in result["questions"])


def test_process_flags_bracelet_intersection():
    src_id = _ingest("стоматолог предложил новокаин перед лечением зуба")
    with patch("app.write_path.extract", return_value=_mock_extract(no_medical=True)):
        result = process(src_id)
    assert "allergy:novocaine_anaphylaxis" in result["flags"]["bracelet_hits"]
    assert any("браслетом" in q for q in result["questions"])


# --- F4 (внешний аудит логики, 2026-09-22): закрывающая реплика без открытого эпизода ---

def test_process_closure_without_open_episode_is_noop():
    """«Прошло» по теме, которой в карте нет (или уже закрытой), НЕ создаёт
    новый открытый эпизод — раньше создавало (инверсия смысла)."""
    src = _ingest("голова уже не болит, всё прошло")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache", closure=True)])):
        r = process(src)

    assert r["written"][0]["action"] == "skipped_closing_without_open_episode"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.episode WHERE symptom_key = 'headache'")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {schema()}.fact WHERE metric_key = 'symptom:headache'")
        assert cur.fetchone()[0] == 0


def test_process_negation_without_open_episode_is_noop():
    """То же для negation («симптома нет») — ветка та же, что у closure."""
    src = _ingest("головной боли нет")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache", negation=True)])):
        r = process(src)

    assert r["written"][0]["action"] == "skipped_closing_without_open_episode"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.episode WHERE symptom_key = 'headache'")
        assert cur.fetchone()[0] == 0


def test_process_closure_closes_existing_open_episode():
    """closure (без negation) тоже закрывает открытый эпизод — раньше поле
    Draft.closure игнорировалось вовсе, закрытие работало только через negation."""
    src1 = _ingest("болит голова")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache")])):
        r1 = process(src1)
    ep_id = r1["written"][0]["episode_id"]

    src2 = _ingest("тема закрыта, всё прошло")
    with patch("app.write_path.extract", return_value=_mock_extract([Draft(symptom_key="headache", closure=True)])):
        r2 = process(src2)

    assert r2["written"][0]["action"] == "closed_episode"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, closure_source FROM {schema()}.episode WHERE id = %s", (ep_id,))
        assert cur.fetchone() == ("resolved", "user")


# --- F2 (внешний аудит логики, 2026-09-22): красный флаг на TEST-пути -> алерт ---

def test_process_alerts_owner_on_red_flag_test_path(monkeypatch):
    """Красный флаг, найденный на TEST-пути, раньше уходил только в
    card.extraction.flags_json без потребителя — теперь алерт владельцу."""
    import app.write_path as wp
    alerts = []
    monkeypatch.setattr(wp.notify, "notify",
                        lambda source, priority, text: alerts.append((source, priority, text)))

    src_id = _ingest("запиши: сегодня была рвота кровью, кофейной гущей")
    with patch("app.write_path.extract", return_value=_mock_extract(no_medical=True)):
        result = process(src_id)

    assert result["flags"]["red_flag"]["hit"] is True
    assert len(alerts) == 1
    source, priority, text = alerts[0]
    assert priority == "red_flag"
    assert "Красные флаги" in text
    assert "кофейной гущей" in text  # фрагмент исходного текста — владелец видит контекст


def test_process_survives_red_flag_alert_failure(monkeypatch):
    """Сбой отправки алерта не должен ломать обработку — флаг всё равно в результате."""
    import app.write_path as wp

    def boom(*a, **k):
        raise RuntimeError("hermes down")

    monkeypatch.setattr(wp.notify, "notify", boom)

    src_id = _ingest("грудь давит, отдаёт в левую руку, одышка")
    with patch("app.write_path.extract", return_value=_mock_extract(no_medical=True)):
        result = process(src_id)  # не бросает

    assert result["flags"]["red_flag"]["hit"] is True
