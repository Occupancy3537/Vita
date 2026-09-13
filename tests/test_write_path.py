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


def test_process_flags_red_flag_independent_of_llm():
    """Слой A срабатывает по сырому тексту ДО извлечения — даже если LLM решит,
    что содержания нет, красный флаг не теряется."""
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
