-- Follow-up по открытым сессиям красных флагов L2/L3 (2026-09-30).
-- card.rf_followup — идемпотентность follow-up'а: одна строка на сессию,
--_attempts защищает от бесконечных повторов при недоступности Telegram
-- (не чаще 3 попыток на сессию, см. app/redflag_followup.py).
-- Применяется и к card_test (зеркало схемы — tests/conftest.py TABLES_TO_CLEAN).

CREATE TABLE IF NOT EXISTS card.rf_followup (
    session_id text PRIMARY KEY REFERENCES card.rf_session(id),
    sent_ts timestamptz,
    attempts int NOT NULL DEFAULT 0,
    last_error text
);
