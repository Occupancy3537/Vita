-- Замеры шагов в течение дня (2026-10-01): живой счётчик health.live_steps_today хранит только последнее значение,
-- поэтому «ход дня» (кривая шагов) копится отдельно — по одной строке на каждое обновление intervals.icu.
CREATE TABLE IF NOT EXISTS card.steps_sample (
    ts timestamptz PRIMARY KEY,           -- момент обновления live_steps_today (updated_at)
    date date NOT NULL,                    -- локальная дата
    steps integer NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_sample_date_idx ON card.steps_sample (date);
