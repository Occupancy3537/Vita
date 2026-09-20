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
