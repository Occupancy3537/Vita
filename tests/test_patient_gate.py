"""app/patient_gate.py — премортем (2026-09-20, проблема #4 "тройное
дублирование медицинской gate-логики"). HERNIA_RX + правило "когда грыжа
считается снятой" были буквально скопированы в dashboard.py и
weekly_advisor.py — вынесены сюда как единственный источник истины."""
from app import patient_gate as pg


def test_profile_hernia_active_detects_mention():
    assert pg.profile_hernia_active("Грыжа L5/S1, радикулопатия справа") is True


def test_profile_hernia_active_false_when_no_mention():
    assert pg.profile_hernia_active("Без особенностей") is False
    assert pg.profile_hernia_active(None) is False
    assert pg.profile_hernia_active("") is False


def test_profile_hernia_active_false_when_remission_mentioned():
    assert pg.profile_hernia_active("Грыжа L5/S1, ремиссия с 2026-06") is False
    assert pg.profile_hernia_active("Грыжа снята") is False
    assert pg.profile_hernia_active("Грыжа, полное восстановление подтверждено") is False
    assert pg.profile_hernia_active("Грыжа, разрешена нагрузка") is False


def test_profile_swim_allowed():
    assert pg.profile_swim_allowed("ходьба, плавание, бассейн") is True
    assert pg.profile_swim_allowed("только ходьба") is False
    assert pg.profile_swim_allowed(None) is False


def test_dashboard_and_weekly_advisor_agree_on_the_shared_regex():
    """Регрессия премортема: раньше это были ДВЕ копии одного и того же
    текста регэкспа в разных файлах — теперь оба модуля физически используют
    один и тот же compiled pattern, разойтись негде."""
    from app import dashboard, weekly_advisor
    assert dashboard.profile_hernia_active is pg.profile_hernia_active
    assert weekly_advisor.profile_hernia_active is pg.profile_hernia_active
    assert dashboard.profile_swim_allowed is pg.profile_swim_allowed
    assert weekly_advisor.profile_swim_allowed is pg.profile_swim_allowed


def test_dashboard_and_weekly_advisor_use_the_same_load_gate():
    """Унификация 2026-09-21 (AGENT_SYNC #38/#39): раньше _load_gate жила
    только в dashboard.py, а weekly_advisor.py пересчитывала решение о
    блокировке нагрузки САМА через active_restrictions — и расходилась на
    конкретном входе (см. ниже). Теперь оба модуля физически зовут одну
    функцию."""
    from app import dashboard, weekly_advisor
    assert dashboard._load_gate is pg.load_gate
    assert weekly_advisor.load_gate is pg.load_gate


PROFILE_HERNIA = {"ОДА и неврология": "Грыжа L5/S1, радикулопатия"}


def test_load_gate_blocks_on_active_row_with_contra_load():
    pstate = [{"Status": "active", "Condition": "Грыжа L5/S1", "Contra_Load": "бег, прыжки",
               "Allowed": "ходьба", "Confirmed_Date": "2026-06-26"}]
    gate = pg.load_gate(pstate, {})
    assert gate["blocked"] is True
    assert gate["cap"] == 1
    assert gate.get("degraded") is not True


def test_load_gate_swim_allowed_raises_cap():
    pstate = [{"Status": "active", "Contra_Load": "бег", "Allowed": "ходьба, плавание", "Confirmed_Date": "2026-06-26"}]
    assert pg.load_gate(pstate, {})["cap"] == 2


def test_load_gate_empty_pstate_is_degraded():
    gate = pg.load_gate([], {})
    assert gate["blocked"] is True
    assert gate["degraded"] is True


def test_load_gate_no_restriction_when_nothing_active_and_no_hernia():
    pstate = [{"Status": "resolved", "Contra_Load": "бег"}]
    gate = pg.load_gate(pstate, {"ОДА и неврология": "без особенностей"})
    assert gate["blocked"] is False
    assert gate["cap"] == 4


def test_load_gate_active_row_without_contra_load_falls_back_to_profile():
    """Ровно находка независимого аудита (AGENT_SYNC #38, §4.6): единственная
    активная запись Patient_State БЕЗ Contra_Load (например, там указан
    только Contra_Food) не должна выглядеть как "ограничений на нагрузку
    нет" — если профиль подтверждает ещё не снятую грыжу, гейт обязан
    заблокировать нагрузку так же, как если бы Patient_State был вообще
    пуст. До унификации именно этот вход расходился между dashboard.py
    (блокировал) и weekly_advisor.py (пропускал load_high)."""
    pstate = [{"Status": "active", "Condition": "Непереносимость лактозы",
               "Contra_Food": "молочное", "Confirmed_Date": "2026-08-01"}]
    gate = pg.load_gate(pstate, PROFILE_HERNIA)
    assert gate["blocked"] is True
    assert gate["cap"] == 1
    assert "грыж" in gate["condition"].lower() or "радикулопат" in gate["condition"].lower()
