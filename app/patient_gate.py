"""Общие примитивы гейта нагрузки при активной грыже L5/S1 — премортем
(2026-09-20, задача Влада "1,3,4,5,7", проблема #4 "тройное дублирование
медицинской gate-логики"). До этого HERNIA_RX и правило "когда считать
грыжу снятой" были буквально скопированы в dashboard.py (_load_gate,
today-dashboard) и weekly_advisor.py (active_restrictions-блок) — при
правке одной копии (например, добавить новую формулировку ремиссии) риск
незаметно поправить только одну из двух.

НЕ полная унификация: dashboard.py::_load_gate() и weekly_advisor.py's
active_restrictions-блок возвращают структурно РАЗНЫЕ вещи — один гейт-
скаляр (cap/blocked/source) для карточки "сегодня", другой список
ограничений для построчной фильтрации LLM-предложенных действий — и
продолжают жить раздельно: сливать их ради унификации формы рискованнее,
чем стоит (это медицинская safety-логика, трогать форму без явной нужды —
не по бюджету сложности). Общий здесь только сам регэксп "это про грыжу" и
условие "она уже снята" — то немногое, что было БУКВАЛЬНО одинаковым
текстом в обоих файлах и реально могло разойтись."""
import re

HERNIA_RX = re.compile(r"грыж|радикулопат|протру[зи]|экструз|модик|modic|корешк", re.I)
REMISSION_RX = re.compile(r"ремисси|снят|полн(ое|ая) восстановлен|разрешена нагрузка", re.I)
SWIM_RX = re.compile(r"плаван|бассейн", re.I)


def profile_hernia_active(oda_text) -> bool:
    """Упомянута ли в свободном тексте профиля («ОДА и неврология») ещё не
    снятая грыжа/радикулопатия."""
    text = str(oda_text or "")
    return bool(HERNIA_RX.search(text)) and not REMISSION_RX.search(text)


def profile_swim_allowed(oda_text) -> bool:
    """Упомянуто ли плавание/бассейн как разрешённая нагрузка в тексте профиля."""
    return bool(SWIM_RX.search(str(oda_text or "")))


def load_gate(pstate: list[dict], profile: dict) -> dict:
    """Гейт безопасности нагрузки (A1) — ЕДИНАЯ реализация решения
    "заблокировать нагрузку да/нет", используемая и dashboard.py, и
    weekly_advisor.py (унификация 2026-09-21, AGENT_SYNC #38/#39: до этого
    два модуля независимо переизобретали эту логику и расходились на входе
    «единственная активная запись Patient_State без Contra_Load (например,
    только Contra_Food) + грыжа упомянута в профиле» — dashboard блокировал
    нагрузку (проверял профиль как фолбэк всегда, когда среди активных
    записей нет ни одной с Contra_Load), а weekly_advisor не блокировал
    (его фолбэк на профиль срабатывал, только если active_restrictions был
    пуст целиком — а он не пуст, там просто нет строки с contra_load).
    Портировано 1:1 из dashboard.py::_load_gate() — она была корректнее.

    Fail-safe: нет данных о снятии → ограничение действует. Уровни:
    0 отдых · 1 ходьба · 2 +плавание · 3 умеренная аэробика · 4 интенсив.
    Возвращает {cap, blocked, degraded, condition, contra, allowed,
    provokers, review_due, source} — degraded=True только когда сам
    Patient_State пуст (синк не отработал), не когда просто нет активных
    ограничений нагрузки.
    """
    active = [x for x in pstate
              if str(x.get("Status") or "").lower() == "active" and str(x.get("Contra_Load") or "").strip()]
    if active:
        x = max(active, key=lambda r: str(r.get("Confirmed_Date") or ""))
        swim = bool(SWIM_RX.search(str(x.get("Allowed") or "")))
        return {
            "cap": 2 if swim else 1, "blocked": True, "condition": x.get("Condition"),
            "contra": x.get("Contra_Load"), "allowed": x.get("Allowed") or "",
            "provokers": x.get("Provokers") or "", "review_due": x.get("Review_Due"),
            "source": (x.get("Source") or "карта пациента") + (f" от {x['Confirmed_Date']}" if x.get("Confirmed_Date") else ""),
        }

    oda = str(profile.get("ОДА и неврология") or profile.get("ОДА и неврология ") or "")
    prof_hernia = profile_hernia_active(oda)
    prof_swim = prof_hernia and profile_swim_allowed(oda)

    # A6 fail-safe (ревью Opus 5, 2026-09-09): 0 строк в Patient_State = чтение
    # НЕ ПРОШЛО (синк не отработал), а НЕ «ограничений нет».
    if not pstate:
        return {
            "cap": 2 if prof_swim else 1, "blocked": True, "degraded": True,
            "condition": ("Грыжа/радикулопатия — Patient_State не прочитан, профиль подтверждает"
                          if prof_hernia else "Карта пациента не прочитана (Patient_State пуст)"),
            "contra": "осевая нагрузка, подъём тяжестей, скручивания, бег, прыжки, интервалы",
            "allowed": "ходьба" + (", плавание" if prof_swim else ""),
            "provokers": "", "review_due": None,
            "source": "⚠️ PATIENT_STATE НЕ ПРОЧИТАН" + (" (профиль подтверждает грыжу)" if prof_hernia else ""),
        }

    if prof_hernia:
        return {
            "cap": 2 if prof_swim else 1, "blocked": True,
            "condition": "Грыжа/радикулопатия (из профиля; в Patient_State активных ограничений нет)",
            "contra": "осевая нагрузка, подъём тяжестей, скручивания, бег/прыжки/интенсив",
            "allowed": "ходьба" + (", плавание" if prof_swim else ""),
            "provokers": "", "review_due": None,
            "source": "User_Profile (Patient_State без активных ограничений)",
        }

    return {"cap": 4, "blocked": False, "condition": None, "contra": None,
            "allowed": "", "provokers": "", "review_due": None, "source": None}
