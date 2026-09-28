-- 0003_vita_v2.down.sql
DROP TABLE IF EXISTS card.vita_manual_mark;
DROP TABLE IF EXISTS card.vita_day_snapshot;
ALTER TABLE health.user_profile DROP COLUMN IF EXISTS vita_chip_norm;
