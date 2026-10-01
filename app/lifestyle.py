# -*- coding: utf-8 -*-
"""Вклад образа жизни в биовозраст (PhenoAge) — формулы, вынесенные из app/dashboard.py (2026-10-01, фаза 6 плана).

Оценка направления и порядка величины по поведенческой литературе, НЕ пересчёт PhenoAge: настоящий биовозраст считается только
по крови. Один модуль считает и «сегодня» (старый дашборд, ring.bioage_days), и любой закрытый день («Вклад дня» в Vita),
поэтому формулы не расходятся. Чистые функции — без БД."""

CRP_SENS, MCV_SENS, CRP_REF = 1.041, 0.292, 1.5
DEFAULT_ZONE_MIN = (420, 540)


def round4(x):
    return round(x * 10000) / 10000


def effects(sleep_min, steps, steps_target, alcohol_g, fiber_b, sat_fat_b, sugar_b, zone=DEFAULT_ZONE_MIN):
    """Список факторов дня: {what, how, markers, direction, weight, est_years}. Входы None — фактор пропускается.
    fiber_b/sat_fat_b/sugar_b — строки бюджета дня (label, cap, consumed, pct) как из _budget_for_day."""
    affects = []

    if sleep_min is not None:
        lo, hi = zone
        years, what = 0, None
        if sleep_min < lo:
            years = 0.582 * min(1, (lo - sleep_min) / 180) / 365
            what = f"сон {sleep_min / 60:.1f} ч — короче нормы"
        elif sleep_min > hi:
            years = 0.694 * min(1, (sleep_min - hi) / 120) / 365
            what = f"сон {sleep_min / 60:.1f} ч — длиннее нормы"
        else:
            what = "сон в зоне 7–9 ч"
        affects.append({"key": "sleep",
            "what": what,
            "how": "Короткий/длинный сон системно повышает СРБ (воспалительный маркер формулы) — но нужно ≥3 ночи подряд, разовая ночь почти не в счёт. Источник: Ballesio 2025, You 2024 (NHANES).",
            "markers": ["crp"], "direction": "up" if years > 0.00005 else "down" if years < -0.00005 else "neutral",
            "weight": "unknown" if years == 0 else "moderate", "est_years": None if years == 0 else round4(years),
        })

    if steps is not None:
        delta_steps = steps - steps_target
        years = -3.98 * ((delta_steps / 100) / 30) / 365
        affects.append({"key": "steps",
            "what": f"{'+' if delta_steps >= 0 else ''}{round(delta_steps)} шагов к норме {steps_target}",
            "how": "Замена сидения на движение снижает СРБ/лейкоциты/RDW — самая воспроизводимая связь в базе (2 независимых NHANES-анализа). Источник: Han 2023.",
            "markers": ["crp", "wbc", "rdw"], "direction": "up" if years > 0.00005 else "down" if years < -0.00005 else "neutral",
            "weight": "strong", "est_years": round4(years),
        })

    if alcohol_g is not None:
        mcv_shift_fl = (0.30 * (alcohol_g / 40) / 100) * 88
        years = (mcv_shift_fl * MCV_SENS) / (90 / 7)
        affects.append({"key": "alcohol",
            "what": f"{alcohol_g} г алкоголя вчера" if alcohol_g > 0 else "без алкоголя вчера",
            "how": "Алкоголь линейно повышает MCV — причинная связь (менделевская рандомизация, UK Biobank). Эффект накапливается за ~90 дней оборота эритроцитов. Источник: Thompson 2021.",
            "markers": ["mcv"], "direction": "up" if years > 0.00002 else "neutral",
            "weight": "moderate" if alcohol_g > 0 else "unknown", "est_years": round4(years),
        })

    if fiber_b:
        gap_g = fiber_b["cap"] - fiber_b["consumed"]
        years = (gap_g / 8) * (0.37 * CRP_SENS / CRP_REF) / 42
        affects.append({"key": "fiber",
            "what": f"клетчатка {fiber_b['consumed']}/{fiber_b['cap']} г",
            "how": "Клетчатка снижает СРБ — подтверждено в 7+ независимых RCT/метаанализах. Источник: Jiao 2015, Jain 2025.",
            "markers": ["crp"], "direction": "up" if years > 0.00002 else "down" if years < -0.00002 else "neutral",
            "weight": "moderate", "est_years": round4(years),
        })

    if sat_fat_b and sugar_b:
        proxy_pct = (sat_fat_b["pct"] - 100) + (sugar_b["pct"] - 100)
        years = (0.21 * proxy_pct / 10) / 365
        affects.append({"key": "fatsugar",
            "what": f"насыщ. жиры {sat_fat_b['pct']}%, сахар {sugar_b['pct']}% от лимита",
            "how": "Хронический избыток насыщенных жиров/сахара связан с ростом PhenoAge через глюкозу и слабее СРБ, но за один день эффект почти не заметен (нужны недели) — самый слабый по доказательности пункт формулы. Источник: Cardoso 2024.",
            "markers": ["gluc", "crp"], "direction": "up" if years > 0.00002 else "down" if years < -0.00002 else "neutral",
            "weight": "weak", "est_years": round4(years),
        })

    return affects
