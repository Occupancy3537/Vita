-- «Мои цели» (2026-10-01, фаза 5 плана): переопределения целей Влада поверх базовых значений
-- (профиль питания, справочник норм, константы кода) + жёсткие рамки врача.
-- value NULL = переопределения нет (действует базовое); frame_lo/frame_hi — рамка врача: выйти за неё нельзя.
CREATE TABLE IF NOT EXISTS card.goal (
    key text PRIMARY KEY,
    value numeric,
    source text NOT NULL DEFAULT 'я',
    frame_lo numeric,
    frame_hi numeric,
    frame_source text,
    frame_note text,
    prev_value numeric,
    set_ts timestamptz NOT NULL DEFAULT now()
);
-- Рамки, которые врач уже задал (по записям консилиума и ограничениям L5/S1)
INSERT INTO card.goal (key, source, frame_hi, frame_source, frame_note) VALUES
  ('sat_fat_g', 'по умолчанию', 28, 'консилиум 30 сент.', 'Насыщенные жиры не больше 28 г в день'),
  ('stand_gap_min', 'по умолчанию', 40, 'ограничения L5/S1', 'Перерыв без движения не дольше 40 минут')
ON CONFLICT (key) DO NOTHING;
