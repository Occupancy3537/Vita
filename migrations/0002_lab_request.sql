-- 0002_lab_request.sql — тикет «оптимизатор сдачи анализов» (2026-09-26).
--
-- card.lab_request — ТОЛЬКО явные одноразовые запросы, которые нельзя вывести
-- живым запросом из уже существующих сущностей: ручная регистрация через
-- POST /labs/request (будущий доктор/визит/консилиум-путь, см. RUNBOOK).
-- Стоящие интервальные правила (PhenoAge-панель, каталог card/health -
-- см. app/lab_catalog.py) и запросы от рекомендаций/интервенций движок
-- вычисляет НА ЛЕТУ из card.recommendation/card.expectation/card.intervention
-- при каждом расчёте плана — не дублируются здесь как отдельные строки
-- (см. докстринг app/lab_optimizer.py).
CREATE TABLE card.lab_request (
    id text PRIMARY KEY,
    ts_recorded timestamp with time zone NOT NULL DEFAULT now(),
    marker_code text NOT NULL,
    source_type text NOT NULL,
    source_id text,
    source_ref text,
    reason text,
    requested_ts timestamp with time zone NOT NULL DEFAULT now(),
    due_date date,
    urgent boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'open',
    fulfilled_by_result_id text,
    CONSTRAINT lab_request_status_check CHECK (status IN ('open', 'fulfilled', 'superseded')),
    CONSTRAINT lab_request_source_type_check CHECK (
        source_type IN ('visit', 'consilium', 'recommendation', 'intervention_monitor', 'phenoage_panel', 'standing')
    )
);

CREATE INDEX lab_request_open_idx ON card.lab_request (marker_code) WHERE status = 'open';

-- card.lab_reminder_sent — дедуп напоминания «панель созревает через 3 дня»
-- (Часть 3.3): окно проверки шире одного дня (0..3 дня до даты панели, не
-- ровно 3), чтобы пропуск одного прогона планировщика не терял напоминание
-- молча — INSERT ... ON CONFLICT DO NOTHING гарантирует ровно одно
-- уведомление на дату панели, даже если проверка идёт каждый день подряд.
CREATE TABLE card.lab_reminder_sent (
    panel_date date PRIMARY KEY,
    sent_at timestamp with time zone NOT NULL DEFAULT now()
);
