-- Цены лабораторий (этап 0 плана docs/PRICES_PLAN_QWEN.md, 2026-09-29).
-- card.lab — справочник лаб (источник — сводный JSON скрейпа, поле lab_code);
-- card.lab_item — позиции прайса (single или complex), еженедельно
-- перезаливается загрузчиком (app/lab_prices_ingest.py), ON CONFLICT —
-- апдейт цены/даты без накопления истории строк (дисклеймер свежести —
-- parsed_at на строке; история цен сознательно не ведётся).
--
-- Покрытие каталожными кодами (M0xx) в БД НЕ хранится: маппинг живёт в коде
-- app/lab_prices_map.py — по образцу lab_catalog («правки — правкой правил,
-- не ручной правкой плана»): git-история, код-ревью, тест на валидность кодов
-- (tests/test_lab_prices.py). Позиции без маппинга в расчёт цен не попадают —
-- молчаливого матчинга нет по построению.

CREATE TABLE card.lab (
    key  text PRIMARY KEY,           -- 'gemotest' | 'invitro' | 'tafi' | 'unilab'
    name text NOT NULL,
    city text NOT NULL DEFAULT 'Владивосток'
);

CREATE TABLE card.lab_item (
    lab_key       text NOT NULL REFERENCES card.lab(key) ON DELETE CASCADE,
    external_code text NOT NULL,     -- код позиции в прайсе лабы
    name          text NOT NULL,
    category      text,
    kind          text NOT NULL DEFAULT 'single'
                  CHECK (kind IN ('single', 'complex')),
    price_rub     numeric(10, 2) NOT NULL CHECK (price_rub >= 0),
    currency      text NOT NULL DEFAULT 'RUB',
    turnaround    text,              -- срок готовности как в прайсе («1 день», «до 3 раб. дн.»)
    url           text,
    composition   text,              -- строка состава (у комплексов), у синглов '-'
    parsed_at     timestamptz NOT NULL,
    PRIMARY KEY (lab_key, external_code)
);

CREATE INDEX lab_item_lab_idx ON card.lab_item (lab_key);
