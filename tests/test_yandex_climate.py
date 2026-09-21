"""app/yandex_climate.py — порт n8n Get Yandex Climate_2 (2026-09-21, найден
при проверке "можно ли убрать n8n"). Юниты на parse_climate + оркестрацию
run_once с замоканными Yandex API/Sheets вызовами."""
from app import yandex_climate as yc


def _device(properties):
    return {"id": yc.YANDEX_DEVICE_ID, "properties": properties}


def _prop(instance, value):
    return {"parameters": {"instance": instance}, "state": {"instance": instance, "value": value}}


def test_parse_climate_extracts_all_three():
    device = _device([_prop("temperature", 22.5), _prop("humidity", 55), _prop("pm2.5_density", 3)])
    row = yc.parse_climate(device, now="2026-09-21T10:00:00Z")
    assert row == {"Дата": "2026-09-21T10:00:00Z", "Температура": 22.5, "Влажность": 55, "PM2.5": 3}


def test_parse_climate_ignores_unrelated_properties():
    device = _device([_prop("temperature", 20), {"parameters": {"instance": "on"}, "state": {"value": True}}])
    row = yc.parse_climate(device, now="x")
    assert row["Температура"] == 20
    assert row["Влажность"] is None
    assert row["PM2.5"] is None


def test_parse_climate_skips_null_state_values():
    device = _device([{"parameters": {"instance": "temperature"}, "state": {"value": None}}])
    row = yc.parse_climate(device, now="x")
    assert row["Температура"] is None


def test_parse_climate_no_properties_all_none():
    row = yc.parse_climate({}, now="x")
    assert row["Температура"] is None and row["Влажность"] is None and row["PM2.5"] is None


# --- run_once оркестрация -----------------------------------------------------

def test_run_once_writes_when_data_present(monkeypatch):
    monkeypatch.setattr(yc, "wake_device", lambda: None)
    monkeypatch.setattr(yc, "get_device_state", lambda: _device([_prop("temperature", 21)]))
    monkeypatch.setattr(yc.time, "sleep", lambda s: None)
    synced = []
    monkeypatch.setattr(yc, "sync_to_sheet", lambda row: synced.append(row))

    yc.run_once()

    assert len(synced) == 1
    assert synced[0]["Температура"] == 21


def test_run_once_skips_write_when_all_none(monkeypatch):
    monkeypatch.setattr(yc, "wake_device", lambda: None)
    monkeypatch.setattr(yc, "get_device_state", lambda: _device([]))
    monkeypatch.setattr(yc.time, "sleep", lambda s: None)
    synced = []
    monkeypatch.setattr(yc, "sync_to_sheet", lambda row: synced.append(row))

    yc.run_once()
    assert synced == []


def test_run_once_survives_wake_failure(monkeypatch):
    def boom():
        raise ConnectionError("нет связи")
    monkeypatch.setattr(yc, "wake_device", boom)
    monkeypatch.setattr(yc, "get_device_state", lambda: _device([_prop("humidity", 60)]))
    monkeypatch.setattr(yc.time, "sleep", lambda s: None)
    synced = []
    monkeypatch.setattr(yc, "sync_to_sheet", lambda row: synced.append(row))

    yc.run_once()  # не должно упасть, несмотря на сбой wake_device
    assert len(synced) == 1


def test_run_once_survives_device_api_failure(monkeypatch):
    monkeypatch.setattr(yc, "wake_device", lambda: None)

    def boom():
        raise ConnectionError("нет связи")
    monkeypatch.setattr(yc, "get_device_state", boom)
    monkeypatch.setattr(yc.time, "sleep", lambda s: None)
    synced = []
    monkeypatch.setattr(yc, "sync_to_sheet", lambda row: synced.append(row))

    yc.run_once()  # не должно упасть
    assert synced == []
