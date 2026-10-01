-- Консилиум одним итогом (2026-10-01, просьба Влада: «врачи посовещались и решили …», без споров).
-- verdict — итог семейного врача (1–3 предложения). Короткие формулировки действий лежат внутри
-- consilium_report.actions (поле short), отдельной колонки не нужно.
ALTER TABLE card.consilium_report ADD COLUMN IF NOT EXISTS verdict text;
