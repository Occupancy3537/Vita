"""Phase 3 плана нового доктора — context.py. Юниты на фейковом курсоре (health.*
не имеет тестовой схемы-аналога)."""
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


def test_planned_labs_maps_columns():
    """2026-09-19: до этого lab_plan нигде не читался доктором — реальный
    итог, B12 запланирован дважды подряд в двух разговорах. Видимость вместо
    жёсткого правила под конкретный тест."""
    cur = FakeCursor([[
        ("LP-01", "Витамин B12 (сыворотка)", "Витамины", "2026-10-18",
         "Пограничный B12 на фоне радикулопатии", "AI-доктор"),
    ]])
    r = context._planned_labs(cur)
    assert r == [{
        "plan_id": "LP-01", "test": "Витамин B12 (сыворотка)", "category": "Витамины",
        "next_due": "2026-10-18", "reason": "Пограничный B12 на фоне радикулопатии", "source": "AI-доктор",
    }]


def test_planned_labs_respects_limit():
    rows = [(f"LP-{i}", f"Тест {i}", "", None, "", "") for i in range(20)]
    cur = FakeCursor([rows])
    r = context._planned_labs(cur, limit=5)
    assert len(r) == 5


# --- room climate (2026-09-20, #28: health.microclimate, n8n-мост убран) ----

def test_room_climate_maps_columns():
    cur = FakeCursor([[("24,3", "55", "3", "2026-09-19T23:05:31.971Z")]])
    r = context._room_climate(cur)
    assert r == {"temp_c": 24.3, "humidity_pct": 55.0, "pm25": 3.0, "measured_at": "2026-09-19T23:05:31.971Z"}


def test_room_climate_none_when_no_rows():
    assert context._room_climate(FakeCursor([[]])) is None


# --- recent publications (2026-09-25, «научный контур», Часть 5.1) ------------

def test_recent_publications_maps_columns():
    cur = FakeCursor([[
        ("Vitamin D supplementation reduces fall risk in elderly", "meta-analysis", None,
         "у тебя низкий D — та же тема, что твой дефицит", "https://pubmed.ncbi.nlm.nih.gov/1/"),
    ]])
    r = context._recent_publications(cur)
    assert r == [{"title": "Vitamin D supplementation reduces fall risk in elderly",
                  "design_type": "meta-analysis", "phase": None,
                  "why": "у тебя низкий D — та же тема, что твой дефицит",
                  "url": "https://pubmed.ncbi.nlm.nih.gov/1/"}]


def test_recent_publications_respects_limit():
    rows = [(f"T{i}", "trial", "PHASE2", "почему", "u") for i in range(10)]
    cur = FakeCursor([rows])
    r = context._recent_publications(cur, limit=5)
    assert len(r) == 5


# --- gate_status / active_problems (2026-09-26, «досье — тонкое ядро», Часть 1.1) ---

def test_gate_status_blocked_shape(monkeypatch):
    monkeypatch.setattr(context, "load_gate", lambda pstate, profile: {
        "blocked": True, "condition": "Грыжа L5/S1", "contra": "бег, прыжки", "allowed": "ходьба, плавание",
    })
    r = context._gate_status(FakeCursor([[], []]))
    assert r == {"blocked": True, "condition": "Грыжа L5/S1", "contra": "бег, прыжки", "allowed": "ходьба, плавание"}


def test_gate_status_not_blocked_is_minimal(monkeypatch):
    """Не блокирован -> остальные поля не нужны (format_dossier просто не
    покажет секцию) — не тащим condition=None/contra=None в промпт зря."""
    monkeypatch.setattr(context, "load_gate", lambda pstate, profile: {"blocked": False})
    assert context._gate_status(FakeCursor([[], []])) == {"blocked": False}


def test_active_problems_maps_columns():
    cur = FakeCursor([[("p1", "Боль в боку 10 лет", "R10.9", "2026-09-01")]])
    r = context._active_problems(cur)
    assert r == [{"id": "p1", "title": "Боль в боку 10 лет", "icd_hint": "R10.9", "opened": "2026-09-01"}]


def test_active_problems_empty_when_none_active():
    assert context._active_problems(FakeCursor([[]])) == []


# --- route_blocks (Часть 1.2) — детерминированный роутер по ключевым словам ---

def test_route_blocks_symptom_topic():
    blocks = context.route_blocks("Второй день болит голова, тошнит")
    assert {"recent_doctor_notes", "room_climate", "anomaly_dispositions", "garmin_week_trend"} <= blocks


def test_route_blocks_supplement_topic():
    assert context.route_blocks("Можно ли принимать магний вместе с витамином D?") == {
        "labs_out_of_range", "recent_publications"}


def test_route_blocks_nutrition_topic():
    assert context.route_blocks("Что я сегодня ел на завтрак?") == {"nutrition_today", "meals_today"}


def test_route_blocks_labs_topic():
    assert context.route_blocks("Какие у меня были последние анализы крови?") == {
        "labs_out_of_range", "planned_labs"}


def test_route_blocks_science_topic():
    assert context.route_blocks("Есть новые исследования про магний?") == {"recent_publications"}


def test_route_blocks_consilium_topic():
    assert context.route_blocks("Что решили специалисты на консилиуме?") == {"recent_consilium_summaries"}


def test_route_blocks_unrecognized_topic_is_empty():
    assert context.route_blocks("Привет, как дела?") == set()


def test_route_blocks_empty_text_is_empty():
    assert context.route_blocks("") == set()


def test_route_blocks_unions_multiple_matching_rules():
    """Сообщение и про симптом, и про еду — объединяем совпадения, не
    выбираем одну тему произвольно."""
    blocks = context.route_blocks("Живот болит после того как поел")
    assert "recent_doctor_notes" in blocks  # симптом
    assert "nutrition_today" in blocks  # питание


# --- build_dossier (Часть 1.1-1.2): ядро + маршрутизируемые блоки -------------

def test_build_dossier_core_only_when_topic_not_recognized(monkeypatch):
    monkeypatch.setattr(context, "get_context", lambda cur, mode, payload: {"stub": True})
    monkeypatch.setattr(context, "load_gate", lambda pstate, profile: {"blocked": False})
    # порядок внутри ядра: gate(2 запроса) + active_problems + active_meds +
    # garmin_yesterday + open_investigations = 6 execute()
    cur = FakeCursor([[], [], [], [], [], []])
    d = context.build_dossier(cur, "Привет, как дела?")
    assert set(d.keys()) == {
        "memory", "gate_status", "active_problems", "active_meds",
        "garmin_yesterday", "open_investigations",
    }
    assert d["memory"] == {"stub": True}


def test_build_dossier_adds_routed_blocks_for_recognized_topic(monkeypatch):
    monkeypatch.setattr(context, "get_context", lambda cur, mode, payload: {"stub": True})
    monkeypatch.setattr(context, "load_gate", lambda pstate, profile: {"blocked": False})
    cur = FakeCursor([[], [], [], [], [], [], [], []])  # 6 ядра + 2 для темы "питание"
    d = context.build_dossier(cur, "Что я сегодня ел?")
    assert "nutrition_today" in d and "meals_today" in d
    assert "labs_out_of_range" not in d and "recent_publications" not in d
    assert "planned_labs" not in d and "recent_consilium_summaries" not in d
