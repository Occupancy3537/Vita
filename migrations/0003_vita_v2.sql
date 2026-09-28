-- 0003_vita_v2.sql — тикет «Vita v2, этап 1» (2026-09-28).
--
-- CHIP_NORM (Часть «Бэкенд-дельта» п.5): пороги сегментов (recovery/sleep/
-- move/food), по которым кругляш красится в "хорошо"/"можно улучшить" —
-- были захардкожены в мокапе (const CHIP_NORM), переносятся в профиль.
ALTER TABLE health.user_profile
    ADD COLUMN vita_chip_norm jsonb NOT NULL
    DEFAULT '{"recovery":65,"sleep":70,"move":70,"food":70}'::jsonb;

-- Снимок дня (Часть «Бэкенд-дельта» п.4): денормализованный слепок ключевых
-- полей экрана "Сегодня" на момент закрытия суток — только чтение, пишется
-- РОВНО один раз в день из finalize_yesterday() (app/nutrition_reports.py).
-- Нужен потому, что часть входов "Сегодня" (health.live_steps_today) не
-- хранит историю сама по себе — без снимка "вчера" был бы нечестным.
CREATE TABLE card.vita_day_snapshot (
    date date PRIMARY KEY,
    ring jsonb NOT NULL,
    chips jsonb NOT NULL,
    nudge jsonb,
    gate jsonb,
    streaks jsonb,
    ts_recorded timestamp with time zone NOT NULL DEFAULT now()
);

-- Ручные отметки дня (Часть «Бэкенд-дельта» п.6): когда часы не видели
-- (no_watch), Влад может отметить руками то, что обычно видит Гармин —
-- source='manual' отличает от гарминовского значения того же дня.
CREATE TABLE card.vita_manual_mark (
    date date NOT NULL,
    field_key text NOT NULL,
    value_bool boolean,
    value_num numeric,
    ts_recorded timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (date, field_key)
);
