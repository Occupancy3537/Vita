from unittest.mock import patch

from fastapi.testclient import TestClient

from app.extraction import Draft, ExtractionResult
from app.main import app

client = TestClient(app)


def test_process_endpoint_end_to_end():
    r1 = client.post("/ingest", json={"channel": "telegram", "raw_text": "болит спина после тренировки"})
    src_id = r1.json()["id"]

    with patch("app.write_path.extract", return_value=ExtractionResult(
        drafts=[Draft(symptom_key="back_pain", onset_expr="после тренировки", intensity=3)]
    )):
        r2 = client.post(f"/process/{src_id}")

    assert r2.status_code == 200
    body = r2.json()
    assert body["written"][0]["action"] == "created_episode"
    assert body["flags"]["red_flag"]["hit"] is False


def test_process_endpoint_unknown_id_404():
    r = client.post("/process/src_does_not_exist")
    assert r.status_code == 404
