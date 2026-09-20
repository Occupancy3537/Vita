"""app/meds_from_calendar.py — порт n8n Card: Meds from Calendar (2026-09-21,
последний пункт группы 1, 4 ноды). Google Calendar credential уже
расшифрован с #34 (app.sheets_client), /interventions/sync — уже
существующий эндпоинт card-service, здесь только фильтр + оркестрация."""
from app import meds_from_calendar as mfc


def test_med_rx_matches_dosage_units():
    assert mfc.MED_RX.search("Магний 400mg")
    assert mfc.MED_RX.search("Витамин D3 5000 IU")
    assert mfc.MED_RX.search("Омега-3 1000 мг")
    assert mfc.MED_RX.search("Что-то 50 мкг")


def test_med_rx_matches_words():
    assert mfc.MED_RX.search("Выпить таблетку")
    assert mfc.MED_RX.search("Капсула вечером")
    assert mfc.MED_RX.search("Утренняя доза")


def test_med_rx_does_not_match_unrelated_events():
    assert not mfc.MED_RX.search("Встреча с коллегами")
    assert not mfc.MED_RX.search("Позвонить маме")
    assert not mfc.MED_RX.search("Планёрка в 15:00")


def test_filter_medication_like_extracts_name_and_source_ref():
    events = [
        {"summary": "Магний 400мг", "id": "evt1", "start": {"dateTime": "2026-09-21T09:00:00+10:00"}},
        {"summary": "Встреча", "id": "evt2", "start": {"dateTime": "2026-09-21T15:00:00+10:00"}},
        {"summary": "Витамин D3 5000 IU", "id": "evt3", "recurringEventId": "rec3", "start": {"date": "2026-09-21"}},
    ]
    out = mfc.filter_medication_like(events)
    assert len(out) == 2
    assert out[0] == {"name": "Магний 400мг", "source_ref": "evt1", "started_ts": "2026-09-21T09:00:00+10:00"}
    # recurringEventId предпочтительнее id, если есть (дедуп по повторяющемуся событию, не по конкретному инстансу)
    assert out[1]["source_ref"] == "rec3"


def test_filter_medication_like_empty_summary_no_crash():
    assert mfc.filter_medication_like([{"id": "x"}]) == []


def test_sync_meds_from_calendar_calls_interventions_sync(monkeypatch):
    from app import main as m

    captured = []

    class FakeResp:
        id = "iv_x"
        created = True

    def fake_sync(req):
        captured.append(req)
        return FakeResp()

    monkeypatch.setattr(m, "interventions_sync", fake_sync)
    events = [{"summary": "Магний 400мг", "id": "evt1", "start": {"dateTime": "2026-09-21T09:00:00+10:00"}}]
    n = mfc.sync_meds_from_calendar(events)
    assert n == 1
    assert captured[0].name == "Магний 400мг"
    assert captured[0].source_ref == "evt1"
    assert captured[0].kind == "supplement"
    assert captured[0].origin == "calendar"


def test_sync_meds_from_calendar_skips_events_without_source_ref(monkeypatch):
    from app import main as m
    calls = []
    monkeypatch.setattr(m, "interventions_sync", lambda req: calls.append(req))
    events = [{"summary": "Таблетка от простуды"}]  # ни id, ни recurringEventId
    n = mfc.sync_meds_from_calendar(events)
    assert n == 0
    assert calls == []


def test_sync_meds_from_calendar_survives_single_failure(monkeypatch):
    from app import main as m

    def boom(req):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(m, "interventions_sync", boom)
    events = [{"summary": "Магний 400мг", "id": "evt1"}]
    n = mfc.sync_meds_from_calendar(events)
    assert n == 0  # не упало наружу, просто не засчиталось


def test_run_once_survives_calendar_failure(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("нет сети")
    monkeypatch.setattr("app.sheets_client.get_calendar_events", boom)
    mfc.run_once()  # не должно бросить исключение
