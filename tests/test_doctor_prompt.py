"""Phase 4 плана нового доктора — prompt.py: soft_probe (обёртка над уже
портированным app.redflag.soft_detect) и рендер досье."""
from app.doctor import prompt


def test_build_soft_probe_empty_when_hard_flag_hit():
    assert prompt.build_soft_probe("болит голова, самая сильная в жизни", hard_flag_hit=True) == ""


def test_build_soft_probe_empty_when_no_topic_matches():
    assert prompt.build_soft_probe("привет, как дела", hard_flag_hit=False) == ""


def test_build_soft_probe_contains_question_for_matched_topic():
    block = prompt.build_soft_probe("началось головокружение второй день", hard_flag_hit=False)
    assert "ОБЯЗАТЕЛЬНО УТОЧНИ" in block
    assert "Головокружение" in block


def test_system_prompt_has_no_hardcoded_allergies():
    """План §3.7/§2.3: единственный источник аллергий — браслет, не текст промпта."""
    assert "новокаин" not in prompt.SYSTEM_PROMPT.lower()
    assert "пенициллин" not in prompt.SYSTEM_PROMPT.lower()


def test_system_prompt_has_iron_rule_and_red_flags():
    assert "ЖЕЛЕЗНОЕ ПРАВИЛО" in prompt.SYSTEM_PROMPT
    assert "103 / 112" in prompt.SYSTEM_PROMPT
    assert "8-800-2000-122" in prompt.SYSTEM_PROMPT


def test_format_dossier_empty_dossier_does_not_crash():
    empty = {
        "memory": {"bracelet": [], "hot": [], "cold": []},
        "garmin_yesterday": None, "garmin_week_trend": {"days": 0},
        "nutrition_today": None, "meals_today": [], "active_meds": [],
        "open_investigations": [], "recent_doctor_notes": [], "labs_out_of_range": [],
        "planned_labs": [], "room_climate": None, "recent_publications": [],
    }
    text = prompt.format_dossier(empty, "2026-09-15")
    assert "2026-09-15" in text
    assert "браслет пуст" in text


def test_format_dossier_includes_meals_and_meds():
    dossier = {
        "memory": {"bracelet": ["новокаин — анафилаксия"], "hot": [], "cold": []},
        "garmin_yesterday": {"date": "2026-09-14", "sleep_min": 450, "sleep_efficiency_pct": 88,
                              "resting_hr": 50, "sleep_score": 85, "spo2_avg": 97, "steps": 10000,
                              "training_kcal": 100, "fasting_before_sleep_h": 2},
        "garmin_week_trend": {"days": 7, "avg_sleep_min": 460, "avg_resting_hr": 51,
                               "avg_steps": 9500, "avg_sleep_score": 84},
        "nutrition_today": {"kcal": 2000, "protein_g": 100, "carbs_g": 200, "fats_g": 70,
                             "caffeine_mg": 150, "alcohol_g": 0, "added_sugar_g": 20},
        "meals_today": [{"time": "08:00", "description": "овсянка", "kcal": 300,
                         "protein_g": 10, "fats_g": 5, "carbs_g": 40}],
        "active_meds": [{"name": "Vitamin D3", "dose": "5000 МЕ", "regimen": "ежедневно", "kind": "supplement"}],
        "open_investigations": [{"inv_id": "inv1", "opened": "2026-09-01", "trigger": "боль",
                                  "hypothesis": "гипотеза"}],
        "recent_doctor_notes": [{"date": "2026-09-10", "category": "ОДА", "note": "заметка"}],
        "labs_out_of_range": [{"marker": "Глюкоза", "value": 6.5, "unit": "ммоль/л",
                               "ref_min": 4.2, "ref_max": 5.0, "date": "2026-08-01"}],
        "planned_labs": [{"plan_id": "LP-01", "test": "Витамин B12 (сыворотка)",
                           "next_due": "2026-10-18", "reason": "пограничный B12", "source": "AI-доктор"}],
        "room_climate": {"temp_c": 22.0, "humidity_pct": 45, "pm25": 5},
        "recent_publications": [{"title": "Vitamin D reduces fall risk", "design_type": "meta-analysis",
                                  "phase": None, "why": "у тебя низкий D", "url": "https://x/1"}],
    }
    text = prompt.format_dossier(dossier, "2026-09-15")
    assert "новокаин — анафилаксия" in text
    assert "08:00 — овсянка" in text
    assert "Vitamin D3" in text
    assert "inv1" in text
    assert "УЖЕ ЗАПЛАНИРОВАННЫЕ АНАЛИЗЫ" in text
    assert "Витамин B12 (сыворотка)" in text
    assert "Глюкоза" in text
    assert "22.0" in text
    assert "СВЕЖИЕ ПУБЛИКАЦИИ" in text
    assert "Vitamin D reduces fall risk" in text
    assert "мета-анализ" in text
    assert "у тебя низкий D" in text


def test_format_dossier_includes_gate_and_active_problems():
    """«Досье — тонкое ядро» (2026-09-26, Часть 1.1) — gate_status/active_problems:
    новые блоки ЯДРА, рендерятся так же, как остальные."""
    dossier = {
        "memory": {"bracelet": [], "hot": [], "cold": []},
        "gate_status": {"blocked": True, "condition": "Грыжа L5/S1", "contra": "бег, прыжки",
                        "allowed": "ходьба, плавание"},
        "active_problems": [{"id": "p1", "title": "Боль в боку 10 лет", "icd_hint": "R10.9", "opened": "2026-09-01"}],
    }
    text = prompt.format_dossier(dossier, "2026-09-26")
    assert "МЕДИЦИНСКОЕ ОГРАНИЧЕНИЕ" in text
    assert "Грыжа L5/S1" in text and "бег, прыжки" in text
    assert "АКТИВНЫЕ ПРОБЛЕМЫ" in text
    assert "Боль в боку 10 лет" in text


def test_format_dossier_gate_not_blocked_has_no_restriction_section():
    dossier = {"memory": {"bracelet": [], "hot": [], "cold": []}, "gate_status": {"blocked": False}}
    text = prompt.format_dossier(dossier, "2026-09-26")
    assert "МЕДИЦИНСКОЕ ОГРАНИЧЕНИЕ" not in text


def test_format_dossier_thin_core_omits_routed_sections():
    """Досье без маршрутизированных блоков (тема не распознана) — секции
    питания/лаб/публикаций/консилиумов просто отсутствуют, не падает."""
    dossier = {
        "memory": {"bracelet": [], "hot": [], "cold": []},
        "gate_status": {"blocked": False}, "active_problems": [], "active_meds": [],
        "garmin_yesterday": None, "open_investigations": [],
    }
    text = prompt.format_dossier(dossier, "2026-09-26")
    for absent in ("ПИТАНИЕ СЕГОДНЯ", "ЛАБЫ ВНЕ РЕФЕРЕНСА", "СВЕЖИЕ ПУБЛИКАЦИИ",
                   "ПОСЛЕДНИЕ ИТОГИ КОНСИЛИУМОВ", "УЖЕ ЗАПЛАНИРОВАННЫЕ АНАЛИЗЫ",
                   "ИСТОРИЯ РЕШЕНИЙ ПО АНОМАЛИЯМ", "КЛИМАТ В КОМНАТЕ"):
        assert absent not in text


def test_format_dossier_includes_consilium_summaries():
    """Консилиум специалистов (2026-09-25, Часть 4.3) — доктор видит, что уже
    решено коллегами, и не переспрашивает заново."""
    dossier = {
        "memory": {"bracelet": [], "hot": [], "cold": []},
        "garmin_yesterday": None, "garmin_week_trend": {"days": 0},
        "nutrition_today": None, "meals_today": [], "active_meds": [],
        "open_investigations": [], "recent_doctor_notes": [], "labs_out_of_range": [],
        "planned_labs": [], "room_climate": None, "recent_publications": [],
        "recent_consilium_summaries": [
            {"topic": "боль в боку 10 лет", "date": "2026-09-25", "status": "completed",
             "actions": [{"imperative": "сделать МРТ пояснично-крестцового отдела", "accepted": True, "id": "r1"}]},
            {"topic": "общий профиль долголетия", "date": "2026-08-01", "status": "empty", "actions": []},
        ],
    }
    text = prompt.format_dossier(dossier, "2026-09-26")
    assert "ПОСЛЕДНИЕ ИТОГИ КОНСИЛИУМОВ" in text
    assert "боль в боку 10 лет" in text
    assert "сделать МРТ пояснично-крестцового отдела" in text
    assert "без новых действий" in text
