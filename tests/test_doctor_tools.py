"""Phase 3 плана нового доктора — tools.py. Юниты на фейковом курсоре (health.*
не имеет тестовой схемы-аналога card_test — эти функции чисто read-only SELECT,
живая построчная сверка с реальными данными уже сделана вручную при разработке,
см. STATE.md; здесь проверяется маппинг колонок и алгоритмическая логика
портированных анализаторов на детерминированных фикстурах, не на проде)."""
from datetime import datetime, timezone

import httpx
import pytest

from app.doctor import tools


class FakeCursor:
    """Отдаёт заготовленные результаты по очереди — один .execute() = один
    следующий элемент из queue (список списков кортежей)."""
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []
        self._current = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self._current = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


def test_read_symptoms_maps_columns():
    cur = FakeCursor([[("sid1", "2026-09-10T12:00", "боль в спине", "ОДА", "3", "active", "усилился", "musculo")]])
    r = tools.read_symptoms(cur, {})
    assert r["symptoms"] == [{
        "symptom_id": "sid1", "ts": "2026-09-10T12:00", "symptom": "боль в спине",
        "system": "ОДА", "severity": "3", "status": "active", "change": "усилился", "domain": "musculo",
    }]


def test_read_investigations_maps_columns():
    cur = FakeCursor([[("inv1", "trig", "detail", "hyp", "open", "find", "brief", "2026-09-01", "2026-09-02")]])
    r = tools.read_investigations(cur, {})
    assert r["investigations"][0]["inv_id"] == "inv1"
    assert r["investigations"][0]["status"] == "open"


def test_get_patient_medical_history_default_limit():
    cur = FakeCursor([[("2026-09-01", "Симптом", "заметка", "триггер", "план")]])
    r = tools.get_patient_medical_history(cur, {})
    assert cur.calls[0][1][0] == 20  # дефолт лимита
    assert r["notes"][0]["note"] == "заметка"


def test_get_patient_medical_history_limit_capped_at_50():
    cur = FakeCursor([[]])
    tools.get_patient_medical_history(cur, {"limit": 999})
    assert cur.calls[0][1][0] == 50


def test_get_meals_today_maps_and_counts():
    cur = FakeCursor([[("08:00", "овсянка", "300", "10", "5", "40"), ("13:00", "суп", "400", "20", "15", "30")]])
    r = tools.get_meals_today(cur, {})
    assert r["count"] == 2
    assert r["meals"][0] == {"time": "08:00", "description": "овсянка", "kcal": 300.0,
                              "protein_g": 10.0, "fats_g": 5.0, "carbs_g": 40.0}


def test_read_labs_out_of_range_and_in_range():
    cur = FakeCursor([[
        ("glucose", "Глюкоза", 6.5, None, "ммоль/л", 4.2, 5.0, datetime(2026, 8, 1)),
    ]])
    r = tools.read_labs(cur, {})
    assert r["labs"][0]["marker"] == "Глюкоза"
    assert r["labs"][0]["value"] == 6.5


def test_read_garmin_history_reverses_to_chronological():
    cur = FakeCursor([[
        ("2026-09-15", "480", "50", "85", "10000", "200"),
        ("2026-09-14", "460", "52", "80", "9000", "150"),
    ]])
    r = tools.read_garmin_history(cur, {"days": 2})
    assert r["days"] == 2
    assert r["history"][0]["date"] == "2026-09-14"  # старое первым
    assert r["history"][1]["date"] == "2026-09-15"


def test_read_garmin_history_days_capped_at_180():
    cur = FakeCursor([[]])
    tools.read_garmin_history(cur, {"days": 9999})
    assert cur.calls[0][1][0] == 180


def test_get_outdoor_weather_calls_open_meteo(monkeypatch):
    captured = {}

    def fake_get(url, timeout=None):
        captured["url"] = url
        class R:
            def raise_for_status(self): pass
            def json(self): return {"current": {"temperature_2m": 15.0}}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    r = tools.get_outdoor_weather(None, {})
    assert "open-meteo.com" in captured["url"]
    assert r["current"]["temperature_2m"] == 15.0


def test_get_room_climate_now_reads_microclimate_table():
    """2026-09-20 (#28): n8n-мост (room-climate-now) убран, прямой SELECT из
    health.microclimate через _room_climate (app.doctor.context)."""
    cur = FakeCursor([[("22,0", "50", "2", "2026-09-19T23:05:31.971Z")]])
    r = tools.get_room_climate_now(cur, {})
    assert r["temp_c"] == 22.0
    assert r["humidity_pct"] == 50.0


def test_get_room_climate_now_no_data():
    cur = FakeCursor([[]])
    r = tools.get_room_climate_now(cur, {})
    assert r == {"error": "no_data"}


# --- Nutrition_Analyzer ------------------------------------------------------

def test_analyze_nutrition_stability_empty_returns_error():
    cur = FakeCursor([[]])
    r = tools.analyze_nutrition_stability(cur, {})
    assert r == {"error": "Нет данных для анализа"}


def test_analyze_nutrition_stability_averages_and_stability():
    def row(day, kcal):
        values = {f: "1" for f in tools.NUTRITION_NUMERIC_FIELDS}
        values["Calories"] = str(kcal)
        return ("self", datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc),
                *[values[f] for f in tools.NUTRITION_NUMERIC_FIELDS])

    # 8 дней данных: maxDate = 9-е (исключается), окно = 1..8 (7 дней внутри 7-дневного лимита)
    rows = [row(d, 2000 + d * 10) for d in range(1, 9)]
    cur = FakeCursor([rows])
    r = tools.analyze_nutrition_stability(cur, {})
    assert r["date"] == "2026-09-08"
    user = r["users"][0]
    assert user["user"] == "self"
    assert user["days_with_data"] == 7
    assert user["calorie_stability_pct"] is not None
    assert 0 <= user["calorie_stability_pct"] <= 100


def test_analyze_nutrition_stability_excludes_max_date_and_older_than_7_days():
    def row(day, kcal=2000):
        values = {f: "0" for f in tools.NUTRITION_NUMERIC_FIELDS}
        values["Calories"] = str(kcal)
        return ("self", datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc),
                *[values[f] for f in tools.NUTRITION_NUMERIC_FIELDS])

    rows = [row(1), row(9)]  # 1-е — вне 7-дневного окна от maxDate=9, 9-е — сам maxDate
    cur = FakeCursor([rows])
    r = tools.analyze_nutrition_stability(cur, {})
    assert r["users"] == []  # обе строки отфильтрованы, не должно быть деления на 0/мусора


# --- Analyze_Symptom_Food ----------------------------------------------------

def test_analyze_symptom_food_no_symptom_id():
    r = tools.analyze_symptom_food(FakeCursor([]), {})
    assert r == {"error": "no_symptom_id"}


def test_analyze_symptom_food_no_episodes():
    cur = FakeCursor([[]])
    r = tools.analyze_symptom_food(cur, {"symptom_id": "sid1"})
    assert r["episodes"] == 0
    assert "нет записанных эпизодов" in r["note"].lower()


def test_analyze_symptom_food_finds_nutrient_candidate():
    base_ts = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)  # вечер
    episodes = [(base_ts, "3", "заметка", "боль")]

    def meal(hour_offset, caffeine, desc="еда"):
        ts = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        ts = ts + timedelta(hours=hour_offset)
        return (ts, desc, "500", "20", "50", "10", "1", "0", "5", str(caffeine), "0", "5", "10", "100")

    # два приёма в pre-окне (6ч до 18:00 = с 12:00) с высоким кофеином, база — низкий
    meals = [
        meal(0, caffeine=200, desc="кофе большой"),
        meal(4, caffeine=200, desc="кофе большой"),
        meal(-10, caffeine=0, desc="салат"),
        meal(-16, caffeine=0, desc="суп"),
        meal(-22, caffeine=0, desc="каша"),
    ]
    cur = FakeCursor([episodes, meals])
    r = tools.analyze_symptom_food(cur, {"symptom_id": "sid1"})
    assert r["episodes"] == 1
    assert r["pre_meals"] == 2
    factors = {c["factor"] for c in r["nutrient_candidates"]}
    assert "Кофеин" in factors


def test_analyze_symptom_food_too_few_pre_meals():
    episodes = [(datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc), "3", None, "боль")]
    meals = [(datetime(2026, 9, 10, 17, 0, tzinfo=timezone.utc), "еда", "500", "20", "50", "10",
              "1", "0", "5", "0", "0", "5", "10", "100")]
    cur = FakeCursor([episodes, meals])
    r = tools.analyze_symptom_food(cur, {"symptom_id": "sid1"})
    assert "мало приёмов пищи" in r["note"].lower()


# --- реестр -------------------------------------------------------------------

def test_tool_registry_names_unique():
    names = [t["name"] for t in tools.TOOL_REGISTRY]
    assert len(names) == len(set(names))


def test_tool_registry_all_have_executor_and_timeout():
    for t in tools.TOOL_REGISTRY:
        assert callable(t["executor"])
        assert t["timeout"] > 0
        assert isinstance(t["read_only"], bool)


def test_tool_registry_read_and_write_split():
    read_names = {t["name"] for t in tools.TOOL_REGISTRY if t["read_only"]}
    write_names = {t["name"] for t in tools.TOOL_REGISTRY if not t["read_only"]}
    assert write_names == {"Record_Symptom", "Record_Note", "Open_Investigation",
                            "Update_Investigation", "Close_Investigation", "Plan_Lab"}
    assert len(read_names) == 10


def test_write_tool_stages_valid_args_without_touching_db():
    r = tools.record_symptom(None, {"symptom_id": "sid1", "symptom": "боль в спине"})
    assert r == {"staged": True, "kind": "symptom",
                 "payload": {"symptom_id": "sid1", "symptom": "боль в спине", "system": None,
                             "severity": None, "status": "active", "change": None, "domain": None,
                             "context": None, "hypothesis": None, "notes": None}}


def test_write_tool_rejects_invalid_args():
    r = tools.record_symptom(None, {"symptom_id": "sid1"})  # symptom обязателен
    assert r.get("error") == "invalid_arguments"


def test_write_tool_severity_out_of_range_rejected():
    r = tools.record_symptom(None, {"symptom_id": "sid1", "symptom": "боль", "severity": 99})
    assert r.get("error") == "invalid_arguments"


def test_open_investigation_stages():
    r = tools.open_investigation(None, {"inv_id": "inv1", "trigger": "жалоба"})
    assert r["staged"] is True
    assert r["kind"] == "investigation_open"
    assert r["payload"]["inv_id"] == "inv1"


def test_plan_lab_stages():
    r = tools.plan_lab(None, {"test": "Глюкоза"})
    assert r["staged"] is True
    assert r["kind"] == "lab_plan"


def test_openai_tool_schemas_shape():
    schemas = tools.openai_tool_schemas()
    assert len(schemas) == len(tools.TOOL_REGISTRY)
    for s in schemas:
        assert s["type"] == "function"
        assert "name" in s["function"] and "description" in s["function"] and "parameters" in s["function"]


def test_analyze_symptom_food_requires_symptom_id_in_schema():
    entry = tools.TOOLS_BY_NAME["Analyze_Symptom_Food"]
    assert entry["parameters"]["required"] == ["symptom_id"]
