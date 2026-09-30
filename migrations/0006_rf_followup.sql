-- Follow-up по открытой сессии красного флага (бриф Влада, 2026-09-30).
-- Система записывает rf_event/rf_session и после этого молчит; здесь — отметка
-- «follow-up отправлен», чтобы часовой планировщик (app/redflag_followup.py)
-- спросил «как сейчас?» РОВНО ОДИН раз на сессию.
--
-- Почему отдельная таблица: у rf_session нет подходящей колонки, расширять
-- объектную таблицу состоянием фонового задания не стоит.
--
-- Идемпотентность по конструкции: session_id PK — повторный прогон не создаст
-- вторую строку; sent_ts IS NULL = ещё не отправлено (только попытки),
-- attempts — счётчик неудачных доставок (максимум 3, см. MAX_ATTEMPTS).
--
-- Применение:
--   прод (Claude):     python3 scripts/migrate.py up
--   card_test:         DDL миграций захардкожен с префиксом card., поэтому:
--                      sed 's/card\./card_test./g' migrations/0006_rf_followup.sql | psql "<card_test DSN>"
--                      (card_test не ведёт _migration_log — это зеркало схемы, см. test_schema_parity)

CREATE TABLE card.rf_followup (
    session_id text PRIMARY KEY REFERENCES card.rf_session(id),
    sent_ts timestamptz,                     -- NULL = ещё не отправлено (идут попытки)
    attempts int NOT NULL DEFAULT 0,         -- счётчик попыток доставки
    last_error text                          -- причина последней неудачи (коротко, без секретов)
);
