"""Phase 3 плана нового доктора — context.py. Юниты на фейковом курсоре (health.*
не имеет тестовой схемы-аналога) + отдельно кэш комнатного климата (план §2.3
оценивал мост в 83мс, живой замер дал ~1.6с — кэш обязателен, не опционален,
см. комментарий у _ROOM_CLIMATE_CACHE_TTL_S)."""
import httpx
import pytest

from app.doctor import context


class FakeCursor:
    def __init__(self, queue):
        self.queue = list(queue)
        self._current = []

    def execute(self, query, params=None):
        self._current = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


def test_num_parses_comma_decimal():
    assert context._num("24,3") == 24.3
    assert context._num("24.3") == 24.3
    assert context._num("") is None
    assert context._num(None) is None
    assert context._num("не число") is None


def test_garmin_yesterday_maps_columns():
    cur = FakeCursor([[("2026-09-15", "459", "88,5", "52", "84", "97", "16811", "221", "1,4", "Vitamin D3")]])
    r = context._garmin_yesterday(cur)
    assert r["date"] == "2026-09-15"
    assert r["sleep_min"] == 459.0
    assert r["sleep_efficiency_pct"] == 88.5
    assert r["meds_seen_in_garmin_note"] == "Vitamin D3"


def test_garmin_yesterday_none_when_no_rows():
    assert context._garmin_yesterday(FakeCursor([[]])) is None


def test_garmin_week_trend_averages():
    cur = FakeCursor([[("480", "50", "10000", "85"), ("460", "52", "9000", "80")]])
    r = context._garmin_week_trend(cur)
    assert r["days"] == 2
    assert r["avg_sleep_min"] == 470.0
    assert r["avg_resting_hr"] == 51.0


def test_garmin_week_trend_zero_days():
    assert context._garmin_week_trend(FakeCursor([[]])) == {"days": 0}


def test_nutrition_today_maps_columns():
    cur = FakeCursor([[("2000", "100", "200", "70", "150", "0", "30")]])
    r = context._nutrition_today(cur)
    assert r == {"kcal": 2000.0, "protein_g": 100.0, "carbs_g": 200.0, "fats_g": 70.0,
                 "caffeine_mg": 150.0, "alcohol_g": 0.0, "added_sugar_g": 30.0}


def test_nutrition_today_none_when_no_rows():
    assert context._nutrition_today(FakeCursor([[]])) is None


def test_meals_today_maps_columns():
    cur = FakeCursor([[("08:00", "овсянка", "300", "10", "5", "40")]])
    r = context._meals_today(cur)
    assert r == [{"time": "08:00", "description": "овсянка", "kcal": 300.0,
                  "protein_g": 10.0, "fats_g": 5.0, "carbs_g": 40.0}]


def test_active_meds_maps_columns():
    cur = FakeCursor([[("Vitamin D3", "5000 МЕ", "ежедневно", "supplement")]])
    r = context._active_meds(cur)
    assert r == [{"name": "Vitamin D3", "dose": "5000 МЕ", "regimen": "ежедневно", "kind": "supplement"}]


def test_open_investigations_maps_columns():
    cur = FakeCursor([[("inv1", "trig", "hyp", "open", "2026-09-01")]])
    r = context._open_investigations(cur)
    assert r == [{"inv_id": "inv1", "trigger": "trig", "hypothesis": "hyp",
                  "status": "open", "opened": "2026-09-01"}]


def test_recent_doctor_notes_maps_columns():
    cur = FakeCursor([[("2026-09-01", "Симптом", "заметка")]])
    r = context._recent_doctor_notes(cur)
    assert r == [{"date": "2026-09-01", "category": "Симптом", "note": "заметка"}]


def test_labs_out_of_range_filters_in_range():
    import datetime
    cur = FakeCursor([[
        ("glucose", "Глюкоза", 6.5, "ммоль/л", 4.2, 5.0, datetime.datetime(2026, 8, 1)),  # выше нормы
        ("chol", "Холестерин", 4.0, "ммоль/л", 3.0, 5.2, datetime.datetime(2026, 8, 1)),   # в норме
    ]])
    r = context._labs_out_of_range(cur)
    assert len(r) == 1
    assert r[0]["marker"] == "Глюкоза"


def test_labs_out_of_range_respects_limit():
    import datetime
    rows = [(f"m{i}", f"M{i}", 100.0, "u", 0.0, 10.0, datetime.datetime(2026, 8, 1)) for i in range(15)]
    cur = FakeCursor([rows])
    r = context._labs_out_of_range(cur, limit=5)
    assert len(r) == 5


# --- room climate cache -------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_climate_cache():
    context._room_climate_cache["value"] = None
    context._room_climate_cache["fetched_at"] = 0.0
    yield
    context._room_climate_cache["value"] = None
    context._room_climate_cache["fetched_at"] = 0.0


def test_room_climate_fetches_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        class R:
            def raise_for_status(self): pass
            def json(self): return {"temp_c": 22.0}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    r1 = context._room_climate()
    r2 = context._room_climate()
    assert r1 == {"temp_c": 22.0}
    assert r2 == {"temp_c": 22.0}
    assert calls["n"] == 1  # второй вызов — из кэша, не сеть


def test_room_climate_refetches_after_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        class R:
            def raise_for_status(self): pass
            def json(self): return {"temp_c": 22.0 + calls["n"]}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(context, "_ROOM_CLIMATE_CACHE_TTL_S", 0)  # мгновенно протухает

    r1 = context._room_climate()
    r2 = context._room_climate()
    assert calls["n"] == 2
    assert r1 != r2


def test_room_climate_falls_back_to_stale_on_error(monkeypatch):
    def fake_get_ok(url, timeout=None):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"temp_c": 22.0}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get_ok)
    first = context._room_climate()
    assert first == {"temp_c": 22.0}

    monkeypatch.setattr(context, "_ROOM_CLIMATE_CACHE_TTL_S", 0)

    def fake_get_fail(url, timeout=None):
        raise httpx.ConnectError("сеть легла")

    monkeypatch.setattr(httpx, "get", fake_get_fail)
    stale = context._room_climate()
    assert stale == {"temp_c": 22.0}  # старое значение лучше None


def test_room_climate_none_when_never_succeeded(monkeypatch):
    def fake_get_fail(url, timeout=None):
        raise httpx.ConnectError("сеть легла")

    monkeypatch.setattr(httpx, "get", fake_get_fail)
    assert context._room_climate() is None


# --- build_dossier ------------------------------------------------------------

def test_build_dossier_has_all_expected_keys(monkeypatch):
    monkeypatch.setattr(context, "get_context", lambda cur, mode, payload: {"stub": True})
    monkeypatch.setattr(context, "_room_climate", lambda: None)
    cur = FakeCursor([[] for _ in range(8)])  # 8 health.*-запросов внутри build_dossier

    d = context.build_dossier(cur, "тест")
    assert set(d.keys()) == {
        "memory", "garmin_yesterday", "garmin_week_trend", "nutrition_today",
        "meals_today", "active_meds", "open_investigations", "recent_doctor_notes",
        "labs_out_of_range", "room_climate",
    }
    assert d["memory"] == {"stub": True}
