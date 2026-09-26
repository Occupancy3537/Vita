-- 0001_baseline.down.sql — откат снимка боевой схемы. Разрушительно по
-- конструкции (DROP SCHEMA ... CASCADE) — scripts/migrate.py отказывается
-- выполнять .down.sql, если current_database() совпадает с прод-базой
-- (CARD_PG_DATABASE в окружении). Предназначен только для одноразовых
-- проверочных баз (см. tests/test_migrations.py — там же используется).
DROP SCHEMA IF EXISTS card CASCADE;
DROP SCHEMA IF EXISTS health CASCADE;
