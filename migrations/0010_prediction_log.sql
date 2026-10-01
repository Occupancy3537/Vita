-- Журнал предсказаний (2026-10-01, фаза 6): что система предсказала и что оказалось, чтобы со временем честно сверять точность.
-- kind='day_index': прогноз индекса дня в 15:00 по Владивостоку → итог закрытого дня (vita_day_snapshot.ring.score).
CREATE TABLE IF NOT EXISTS card.prediction_log (
    kind text NOT NULL,
    target_date date NOT NULL,
    predicted numeric NOT NULL,
    made_ts timestamptz NOT NULL DEFAULT now(),
    observed numeric,
    observed_ts timestamptz,
    meta jsonb,
    PRIMARY KEY (kind, target_date)
);
