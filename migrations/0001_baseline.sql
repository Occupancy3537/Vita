-- 0001_baseline.sql — снимок текущей боевой схемы (card + health), тикет
-- «хвост» (2026-09-26, Часть 1.1). Сгенерирован pg_dump --schema-only,
-- НЕ написан руками — до этой миграции схема жила в 40+ разрозненных
-- файлах backups/infra/*.sql (grants/wave-импорты/точечные ALTER),
-- применявшихся вручную через psql без общего журнала. Эта миграция не
-- отменяет ту историю — она фиксирует её РЕЗУЛЬТАТ как отправную точку,
-- дальше — только через migrations/NNNN_*.sql (см. scripts/migrate.py,
-- RUNBOOK.md «Написать новую миграцию схемы БД»).
--
-- Регенерация (если снова понадобится независимая проверка):
--   docker exec -i pg pg_dump -U n8n -d health --schema-only -n card -n health --no-owner --no-privileges
--
-- PostgreSQL database dump
--

\restrict tPiA4guwBHU416pXlLsrl8CCGvFE7JH2ONmcjZTn6kbgliDqo03mHrk7ynb0r0Z

-- Dumped from database version 17.11
-- Dumped by pg_dump version 17.11

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: card; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA card;


--
-- Name: health; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA health;


--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: health; Owner: -
--

CREATE FUNCTION health.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_step; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.agent_step (
    id text NOT NULL,
    turn_id text,
    step_no integer NOT NULL,
    role text NOT NULL,
    model text,
    tool_name text,
    tool_args jsonb,
    tool_result_hash text,
    latency_ms integer,
    tokens_prompt integer,
    tokens_completion integer,
    tokens_reasoning integer,
    cost_usd double precision,
    ts timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: anomaly_detector_state; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.anomaly_detector_state (
    id integer NOT NULL,
    last_run_at timestamp with time zone NOT NULL,
    last_day_checked date NOT NULL,
    CONSTRAINT anomaly_detector_state_id_check CHECK ((id = 1))
);


--
-- Name: anomaly_disposition; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.anomaly_disposition (
    id text NOT NULL,
    metric_key text NOT NULL,
    metric_label text,
    date date NOT NULL,
    severity text NOT NULL,
    disposition text DEFAULT 'pending'::text NOT NULL,
    reason text,
    suppress_until date,
    investigation_id text,
    hypotheses jsonb,
    disposed_ts timestamp with time zone,
    disposed_by text,
    created_ts timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_person; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.chat_person (
    chat_id text NOT NULL,
    person_id text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: consilium_report; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.consilium_report (
    id text NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    topic text NOT NULL,
    question text,
    trigger text DEFAULT 'command'::text NOT NULL,
    roles jsonb DEFAULT '[]'::jsonb NOT NULL,
    actions jsonb DEFAULT '[]'::jsonb NOT NULL,
    emerging jsonb DEFAULT '[]'::jsonb NOT NULL,
    skeptic_notes jsonb DEFAULT '[]'::jsonb NOT NULL,
    full_text text,
    status text DEFAULT 'completed'::text NOT NULL,
    cost_usd double precision,
    created_ts timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: dialog_turn; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.dialog_turn (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    chat_id text NOT NULL,
    update_id bigint,
    role text NOT NULL,
    text text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    turn_index integer NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    rf_level text,
    wrote_anything boolean DEFAULT false NOT NULL
);


--
-- Name: disagreement; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.disagreement (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    class text,
    opinion_doctor text,
    opinion_advisor text,
    doctor_had text,
    we_have text,
    significance text,
    status text DEFAULT 'raised'::text NOT NULL,
    report_id text
);


--
-- Name: doctor_pending_turn; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.doctor_pending_turn (
    id text NOT NULL,
    chat_id text NOT NULL,
    message_id bigint,
    placeholder_id bigint,
    text text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: entity_index; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.entity_index (
    id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_value text NOT NULL,
    object_id text NOT NULL,
    object_type text NOT NULL,
    weight double precision DEFAULT 1.0 NOT NULL
);


--
-- Name: entity_index_id_seq; Type: SEQUENCE; Schema: card; Owner: -
--

CREATE SEQUENCE card.entity_index_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: entity_index_id_seq; Type: SEQUENCE OWNED BY; Schema: card; Owner: -
--

ALTER SEQUENCE card.entity_index_id_seq OWNED BY card.entity_index.id;


--
-- Name: episode; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.episode (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    symptom_key text NOT NULL,
    onset_ts timestamp with time zone,
    end_ts timestamp with time zone,
    status text DEFAULT 'open'::text NOT NULL,
    intensity integer,
    triggers text[] DEFAULT '{}'::text[] NOT NULL,
    context text,
    problem_id text,
    closure_source text
);


--
-- Name: err_dedup_state; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.err_dedup_state (
    key text NOT NULL,
    last_notified_at timestamp with time zone NOT NULL,
    burst_count integer DEFAULT 0 NOT NULL
);


--
-- Name: expectation; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.expectation (
    id text NOT NULL,
    rec_id text NOT NULL,
    cycle integer DEFAULT 1 NOT NULL,
    metric_key text,
    type text NOT NULL,
    direction text,
    magnitude numeric,
    window_days integer,
    lag_days integer DEFAULT 0 NOT NULL,
    baseline_days integer,
    role text DEFAULT 'primary'::text NOT NULL,
    created_ts timestamp with time zone DEFAULT now() NOT NULL,
    metric_label text,
    unit text,
    reason text,
    freq_min_ratio numeric
);


--
-- Name: extraction; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.extraction (
    id text NOT NULL,
    source_id text NOT NULL,
    model text,
    prompt_version text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    drafts_json jsonb,
    flags_json jsonb,
    status text DEFAULT 'draft'::text NOT NULL,
    reject_reason text
);


--
-- Name: fact; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.fact (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    metric_key text NOT NULL,
    value_num numeric,
    value_text text,
    unit text,
    episode_id text,
    problem_id text,
    attrs jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: host_metrics; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.host_metrics (
    ts timestamp with time zone NOT NULL,
    load1 real,
    load5 real,
    load15 real,
    mem_used_mb integer,
    swap_used_mb integer
);


--
-- Name: intervention; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.intervention (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    kind text NOT NULL,
    name text NOT NULL,
    dose text,
    regimen text,
    started_ts timestamp with time zone,
    ended_ts timestamp with time zone,
    status text DEFAULT 'proposed'::text NOT NULL,
    prescriber text DEFAULT 'self'::text NOT NULL,
    opinion_id text,
    interaction_flags jsonb DEFAULT '[]'::jsonb NOT NULL,
    publication_id text
);


--
-- Name: issue_log; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.issue_log (
    natural_key text NOT NULL,
    source text NOT NULL,
    severity text DEFAULT 'important'::text NOT NULL,
    summary text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    occurrences integer DEFAULT 1 NOT NULL,
    resolved_at timestamp with time zone,
    resolution_ref text,
    CONSTRAINT issue_log_severity_check CHECK ((severity = ANY (ARRAY['critical'::text, 'important'::text, 'minor'::text]))),
    CONSTRAINT issue_log_status_check CHECK ((status = ANY (ARRAY['open'::text, 'snoozed'::text, 'wontfix'::text, 'fixed'::text])))
);


--
-- Name: journal; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.journal (
    j_id text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    object_id text NOT NULL,
    object_type text NOT NULL,
    op text NOT NULL,
    actor text NOT NULL,
    diff jsonb,
    reason text
);


--
-- Name: lab_result; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.lab_result (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'confirmed'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    visit_id text,
    marker_key text NOT NULL,
    marker_label text,
    value_num numeric,
    value_text text,
    unit text,
    ref_min numeric,
    ref_max numeric
);


--
-- Name: llm_usage; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.llm_usage (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    module text NOT NULL,
    model text,
    tokens_prompt integer,
    tokens_completion integer,
    cost_usd numeric(12,6)
);


--
-- Name: llm_usage_id_seq; Type: SEQUENCE; Schema: card; Owner: -
--

ALTER TABLE card.llm_usage ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME card.llm_usage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: memory_note; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.memory_note (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone DEFAULT now() NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    type text NOT NULL,
    title text,
    content jsonb NOT NULL,
    subject jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    last_accessed timestamp with time zone,
    access_count integer DEFAULT 0 NOT NULL
);


--
-- Name: metric_coverage; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.metric_coverage (
    metric_key text NOT NULL,
    observer text NOT NULL,
    frequency text
);


--
-- Name: notify_log; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.notify_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    sent_date date NOT NULL,
    source text NOT NULL,
    priority text NOT NULL,
    immediate boolean NOT NULL,
    text text,
    delivered_in_digest boolean DEFAULT false NOT NULL
);


--
-- Name: notify_log_id_seq; Type: SEQUENCE; Schema: card; Owner: -
--

ALTER TABLE card.notify_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME card.notify_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: opinion; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.opinion (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    author text NOT NULL,
    claim text NOT NULL,
    rationale text,
    rationale_source text DEFAULT 'none'::text NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    subject_type text,
    subject_id text,
    visit_id text,
    status text DEFAULT 'current'::text NOT NULL,
    report_id text
);


--
-- Name: problem; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.problem (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    title text NOT NULL,
    icd_hint text,
    status text DEFAULT 'active'::text NOT NULL,
    opened_ts timestamp with time zone NOT NULL,
    closed_ts timestamp with time zone,
    case_summary jsonb,
    gate jsonb
);


--
-- Name: publication; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.publication (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    source text NOT NULL,
    doi text,
    url text,
    title text NOT NULL,
    abstract_raw text,
    design_type text,
    phase text,
    n integer,
    year integer,
    topic_key text,
    relevant boolean,
    why_for_you text,
    shown_in_digest boolean DEFAULT false NOT NULL
);


--
-- Name: recommendation; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.recommendation (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'auto'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    title text NOT NULL,
    action text,
    rationale text,
    kind text,
    status text DEFAULT 'proposed'::text NOT NULL,
    intervention_id text,
    started_ts timestamp with time zone,
    cycle integer DEFAULT 1 NOT NULL,
    priority text,
    stop_reason text,
    decline_reason text,
    topic_key text,
    publication_id text
);


--
-- Name: recommendation_verdict; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.recommendation_verdict (
    id text NOT NULL,
    rec_id text NOT NULL,
    cycle integer DEFAULT 1 NOT NULL,
    ts_computed timestamp with time zone DEFAULT now() NOT NULL,
    engine_version text NOT NULL,
    verdict text NOT NULL,
    metric_key text,
    baseline_value numeric,
    eval_value numeric,
    basis jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'current'::text NOT NULL,
    role text DEFAULT 'primary'::text,
    personal_sigma numeric,
    coverage jsonb,
    adherence_pct numeric,
    confounded jsonb,
    rule_trace jsonb,
    superseded_by text
);


--
-- Name: research_topic; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.research_topic (
    id bigint NOT NULL,
    topic_key text NOT NULL,
    search_term text NOT NULL,
    label text,
    source text DEFAULT 'manual'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: research_topic_id_seq; Type: SEQUENCE; Schema: card; Owner: -
--

ALTER TABLE card.research_topic ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME card.research_topic_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: rf_event; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.rf_event (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    provenance jsonb NOT NULL,
    category text NOT NULL,
    level text NOT NULL,
    source text NOT NULL,
    rule_ref text,
    source_message_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    session_id text,
    status text DEFAULT 'raised'::text NOT NULL,
    outcome text,
    context_note text,
    confidence double precision,
    journal_ref text
);


--
-- Name: rf_session; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.rf_session (
    id text NOT NULL,
    category text NOT NULL,
    opened_ts timestamp with time zone DEFAULT now() NOT NULL,
    last_activity timestamp with time zone DEFAULT now() NOT NULL,
    worst_level text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    closing_reason text
);


--
-- Name: scheduler_run_log; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.scheduler_run_log (
    name text NOT NULL,
    last_ok_at timestamp with time zone,
    last_error text,
    last_error_at timestamp with time zone
);


--
-- Name: source_message; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.source_message (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    channel text NOT NULL,
    raw_text text,
    ts_received timestamp with time zone DEFAULT now() NOT NULL,
    hash text NOT NULL,
    status text DEFAULT 'received'::text NOT NULL,
    processed_at timestamp with time zone,
    error text,
    process_attempts integer DEFAULT 0 NOT NULL,
    process_error text
);


--
-- Name: telegram_poll_state; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.telegram_poll_state (
    id text DEFAULT 'singleton'::text NOT NULL,
    last_update_id bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: visit; Type: TABLE; Schema: card; Owner: -
--

CREATE TABLE card.visit (
    id text NOT NULL,
    person_id text DEFAULT 'self'::text NOT NULL,
    ts_event timestamp with time zone NOT NULL,
    ts_recorded timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    superseded_by text,
    provenance jsonb NOT NULL,
    verification text DEFAULT 'confirmed'::text NOT NULL,
    salience text DEFAULT 'normal'::text NOT NULL,
    salience_locked boolean DEFAULT false NOT NULL,
    confidence double precision,
    journal_ref text,
    title text,
    raw_text text,
    extraction_status text DEFAULT 'not_started'::text NOT NULL,
    extracted_opinions jsonb DEFAULT '[]'::jsonb NOT NULL
);


--
-- Name: _migration_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health._migration_log (
    id bigint NOT NULL,
    step text NOT NULL,
    detail text,
    ran_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: _migration_log_id_seq; Type: SEQUENCE; Schema: health; Owner: -
--

ALTER TABLE health._migration_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME health._migration_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: action_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.action_log (
    "Action_ID" text NOT NULL,
    "Date_Issued" text,
    "Title" text,
    "Done" text,
    "Done_At" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: anamnesis; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.anamnesis (
    "Q_ID" text NOT NULL,
    "Category" text,
    "Question" text,
    "Status" text,
    "Asked_Date" text,
    "Answer" text,
    "Answered_Date" text,
    "Attempts" integer DEFAULT 0,
    _synced_at timestamp with time zone DEFAULT now()
);


--
-- Name: anomaly_alerted; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.anomaly_alerted (
    key text NOT NULL,
    alert_date date NOT NULL,
    marked_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: anomaly_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.anomaly_log (
    date date NOT NULL,
    anomaly_count integer,
    strong_count integer,
    raw_anomalies jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: backup_alert_state; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.backup_alert_state (
    id integer DEFAULT 1 NOT NULL,
    ts timestamp with time zone,
    result text,
    detail text,
    stamp text,
    CONSTRAINT backup_alert_state_id_check CHECK ((id = 1))
);


--
-- Name: daily_trends; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.daily_trends (
    "Дата" date NOT NULL,
    "Время_отбоя" text,
    "Время_подъема" text,
    "Время_в_кровати_мин" text,
    "Чистый_сон_мин" text,
    "Эффективность_сна_" text,
    "Глубокий_сон_мин" text,
    "Глубокий_1_половина_мин" text,
    "Глубокий_2_половина_мин" text,
    "REM_сон_мин" text,
    "Легкий_сон_мин" text,
    "Пробуждения_кол_во" text,
    "Бодрствование_мин" text,
    "Пульс_ночной_средний" text,
    "Пульс_ночной_мин" text,
    "Пульс_ночной_макс" text,
    "Оценка_сна_балл" text,
    "Беспокойные_моменты" text,
    "Дыхание_ночь_среднее" text,
    "SpO2_ночь_среднее" text,
    "Температура_avg_C" text,
    "Влажность_avg_%" text,
    "PM25_avg" text,
    "Ужин_время" text,
    "Завтрак_время" text,
    "Окно_голода_до_сна_ч" text,
    "Длительность_голода_ч" text,
    "Ужин_Ккал" text,
    "Ужин_Белки_г" text,
    "Ужин_Жиры_г" text,
    "Ужин_Углеводы_г" text,
    "Шаги_за_вчера" text,
    "Тренировка_Ккал" text,
    "Тренировка_поздняя" text,
    "Атмосферное_давление_ночь_hPa" text,
    "Лекарства_принимаемые" text,
    "Время_последнего_кофе" text,
    "Алкоголь_гр" text,
    "Жалобы_вчера" text,
    "Пометки" text,
    "Дефициты" text,
    "Экранное_время_всего_ч" text,
    "Экран_Продуктивно_ч" text,
    "Экран_Отвлечения_ч" text,
    "Восстановление_BodyBattery" text,
    "Стресс_дневной_средний" text,
    "VO2_Max" text,
    "Дыхание_тип" text,
    "Дыхание_мин" text,
    "Экран_перед_сном_мин" text,
    "ВСР_ночная" text,
    "Питание_Всего_Ккал" text,
    "Питание_Всего_Белки_г" text,
    "Питание_Всего_Жиры_г" text,
    "Питание_Всего_Углеводы_г" text,
    "Тренировка_1_Тип" text,
    "Тренировка_1_Мин" text,
    "Тренировка_2_Тип" text,
    "Тренировка_2_Мин" text,
    "Тренировка_3_Тип" text,
    "Тренировка_3_Мин" text,
    "Темп_тренировки_avg_C" text,
    "Темп_тренировки_min_C" text,
    "Темп_тренировки_max_C" text,
    "Training_Status" text,
    "Training_Acute_Load" text,
    "Training_Chronic_Load" text,
    "Точка_росы_avg_C" text,
    "Атм_давление_Дельта_12ч" text,
    "Атм_давление_Дельта_24ч" text,
    "Освещенность_ч_сутки" text,
    "Индекс_когнитивной_нагрузки" text,
    "ACWR_Garmin" text,
    "ACWR_Status" text,
    "Garmin_устройство" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL,
    "Провал_без_движения_мин" text,
    "Плавание_было" text
);


--
-- Name: day_sum; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.day_sum (
    "User_ID" text,
    "Date" date NOT NULL,
    "Calories" text,
    "Proteins" text,
    "Carbs" text,
    "Fats" text,
    "Магний" text,
    "Витамин D" text,
    "Омега-3 (EPA/DHA)" text,
    "Селен" text,
    "Йод" text,
    "Калий" text,
    "Железо" text,
    "Кальций" text,
    "Витамин B12" text,
    "Витамин К" text,
    "Витамин Е" text,
    "Цинк" text,
    "Клетчатка" text,
    "Холестерин" text,
    "Добавленный сахар" text,
    "Натрий" text,
    "Кофеин" text,
    "Алкоголь, гр" text,
    "Насыщенные жиры" text,
    "Трансжиры" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: digest_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.digest_log (
    period_start date NOT NULL,
    period_end date NOT NULL,
    days_with_data integer,
    total_anomalies integer,
    strong_anomalies integer,
    metric_summary text,
    days_detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: doctor_notes; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.doctor_notes (
    id bigint NOT NULL,
    note_date date,
    category text,
    note text,
    trigger text,
    plan text,
    doctor text,
    source text DEFAULT 'AI-доктор'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: doctor_notes_id_seq; Type: SEQUENCE; Schema: health; Owner: -
--

ALTER TABLE health.doctor_notes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME health.doctor_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: garmin_ingest_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.garmin_ingest_log (
    id bigint NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    date text,
    raw_payload jsonb NOT NULL,
    status text DEFAULT 'received'::text NOT NULL,
    error text,
    processed_at timestamp with time zone
);


--
-- Name: garmin_ingest_log_id_seq; Type: SEQUENCE; Schema: health; Owner: -
--

CREATE SEQUENCE health.garmin_ingest_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: garmin_ingest_log_id_seq; Type: SEQUENCE OWNED BY; Schema: health; Owner: -
--

ALTER SEQUENCE health.garmin_ingest_log_id_seq OWNED BY health.garmin_ingest_log.id;


--
-- Name: gate_state; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.gate_state (
    id integer DEFAULT 1 NOT NULL,
    blocked boolean NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gate_state_id_check CHECK ((id = 1))
);


--
-- Name: investigations; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.investigations (
    inv_id text NOT NULL,
    opened date,
    trigger text,
    trigger_detail text,
    hypothesis text,
    status text,
    findings text,
    questions_pending text,
    labs_suggested text,
    doctor_brief text,
    referral text,
    updated date,
    closed date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lab_plan; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.lab_plan (
    "Plan_ID" text NOT NULL,
    "Test" text,
    "Category" text,
    "Interval_Months" text,
    "Last_Done" text,
    "Next_Due" text,
    "Reason" text,
    "Status" text,
    "Source" text,
    "Notes" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: live_steps_today; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.live_steps_today (
    date date NOT NULL,
    steps integer NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    stress integer
);


--
-- Name: COLUMN live_steps_today.stress; Type: COMMENT; Schema: health; Owner: -
--

COMMENT ON COLUMN health.live_steps_today.stress IS 'Средний стресс за день (avg_stress из Garmin, внутридневное; обновляется при синке Garmin, не каждые 20мин как steps)';


--
-- Name: markers; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.markers (
    "Marker_ID" text NOT NULL,
    "Name" text,
    "Category" text,
    "Standard_Unit" text,
    "Optimal_Min" text,
    "Optimal_Max" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: meals; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.meals (
    "Entry_ID" text NOT NULL,
    "User_ID" text,
    "Date" timestamp with time zone,
    "Meal_description" text,
    "Calories" text,
    "Proteins" text,
    "Carbs" text,
    "Fats" text,
    "Магний" text,
    "Витамин D" text,
    "Омега-3 (EPA/DHA)" text,
    "Селен" text,
    "Йод" text,
    "Калий" text,
    "Железо" text,
    "Кальций" text,
    "Витамин B12" text,
    "Витамин К" text,
    "Витамин Е" text,
    "Цинк" text,
    "Клетчатка" text,
    "Холестерин" text,
    "Добавленный сахар" text,
    "Натрий" text,
    "Кофеин" text,
    "Алкоголь" text,
    "Трансжиры" text,
    "Насыщенные жиры" text,
    "NOVA" text,
    veg_g text,
    fruit_g text,
    wholegrain_g text,
    legume_nut_g text,
    redmeat_g text,
    ssb_ml text,
    "ПНЖ" text,
    plants text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: meds; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.meds (
    "Med_ID" text NOT NULL,
    "Name" text,
    "Class" text,
    "Dose" text,
    "Schedule" text,
    "Started" text,
    "Stopped" text,
    "Status" text,
    "Prescribed_By" text,
    "Reason" text,
    "Notes" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: microclimate; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.microclimate (
    "Дата" text NOT NULL,
    "Температура" text,
    "Влажность" text,
    "PM2.5" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: month_sum; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.month_sum (
    month text NOT NULL,
    calc_method text NOT NULL,
    metrics jsonb NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: month_wellness_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.month_wellness_log (
    month text NOT NULL,
    calc_method text NOT NULL,
    metrics jsonb NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: monthly_trend_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.monthly_trend_log (
    id bigint NOT NULL,
    date_computed date NOT NULL,
    period_month text NOT NULL,
    domain text NOT NULL,
    metric text NOT NULL,
    prev_month_mean numeric,
    this_month_mean numeric,
    z numeric,
    n_current integer,
    n_previous integer,
    severity text,
    direction text,
    interpretation text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: monthly_trend_log_id_seq; Type: SEQUENCE; Schema: health; Owner: -
--

CREATE SEQUENCE health.monthly_trend_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: monthly_trend_log_id_seq; Type: SEQUENCE OWNED BY; Schema: health; Owner: -
--

ALTER SEQUENCE health.monthly_trend_log_id_seq OWNED BY health.monthly_trend_log.id;


--
-- Name: nutrient_targets; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.nutrient_targets (
    "Нутриент" text NOT NULL,
    "Колонка_в_Meals" text,
    "Единица" text,
    "Норма_RDA_AI" text,
    "Верхний_предел_UL" text,
    "Категория" text,
    "Источник" text,
    "Примечание" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: nutrition_profile; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.nutrition_profile (
    "User_ID" text NOT NULL,
    "Name" text,
    "Calories_target" text,
    "Protein_target" text,
    "Fat_target" text,
    "Carbs_target" text,
    "Date_of_birth" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patient_state; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.patient_state (
    "State_ID" text NOT NULL,
    "Condition" text,
    "Category" text,
    "Stage" text,
    "Status" text,
    "Confirmed_Date" text,
    "Source" text,
    "Contra_Load" text,
    "Contra_Food" text,
    "Contra_Other" text,
    "Allowed" text,
    "Provokers" text,
    "Review_Every_Days" text,
    "Review_Due" text,
    "Note" text,
    "Updated_At" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: people; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.people (
    id text NOT NULL,
    name text NOT NULL,
    birth_year integer,
    home_tz text DEFAULT 'Asia/Vladivostok'::text NOT NULL,
    current_tz text DEFAULT 'Asia/Vladivostok'::text NOT NULL,
    locale text DEFAULT 'ru'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: phenoage_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.phenoage_log (
    date text NOT NULL,
    chrono_age text,
    phenoage text,
    delta text,
    markers_used text,
    missing text,
    formula_version text NOT NULL,
    contributions text,
    marker_values text,
    oldest_marker_date text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: recommendations_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.recommendations_log (
    "Date" text,
    "Period_Type" text,
    "Recommendation_Text" text,
    "Based_On" text,
    "Status" text,
    "Priority" text,
    "Telegram_Text" text,
    "Alert_Text" text,
    "Has_Alert" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL,
    _nat_key text GENERATED ALWAYS AS (((COALESCE("Date", ''::text) || '|'::text) || "left"(regexp_replace(COALESCE("Recommendation_Text", ''::text), '\s+'::text, ' '::text, 'g'::text), 40))) STORED
);


--
-- Name: results; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.results (
    "Visit_ID" text NOT NULL,
    "Marker_ID" text NOT NULL,
    "Value" text,
    "Original_Unit" text,
    "Lab_Min" text,
    "Lab_Max" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: symptom_log; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.symptom_log (
    id bigint NOT NULL,
    symptom_id text NOT NULL,
    ts timestamp with time zone NOT NULL,
    symptom text,
    system text,
    severity text,
    status text,
    change text,
    domain text,
    context text,
    hypothesis text,
    notes text,
    source text DEFAULT 'AI-доктор'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: symptom_log_id_seq; Type: SEQUENCE; Schema: health; Owner: -
--

ALTER TABLE health.symptom_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME health.symptom_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: user_profile; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.user_profile (
    "User_ID" text NOT NULL,
    "Telegram_ID" text,
    "Year of birth" text,
    "Location" text,
    "Chronic_Conditions" text,
    "Gender" text,
    "Height, sm" text,
    "Allergies" text,
    "Goals" text,
    "Weight, kg" text,
    "Психологический профиль" text,
    "Сон и нервная система (HRV)" text,
    "ОДА и неврология" text,
    "Сердечно-сосудистая система" text,
    "Телосложение" text,
    "Курение" text,
    "Кожа" text,
    "Волосы" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: visits; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.visits (
    "Visit_ID" text NOT NULL,
    "Date" text,
    "Age_at_Visit" text,
    "Lab_Name" text,
    "Notes" text,
    _synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: watchdog_nudged; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.watchdog_nudged (
    key text NOT NULL,
    marked_at text
);


--
-- Name: watchdog_state; Type: TABLE; Schema: health; Owner: -
--

CREATE TABLE health.watchdog_state (
    id integer DEFAULT 1 NOT NULL,
    last_reviewed_visit text,
    last_read_fail_notice text,
    CONSTRAINT watchdog_state_id_check CHECK ((id = 1))
);


--
-- Name: entity_index id; Type: DEFAULT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.entity_index ALTER COLUMN id SET DEFAULT nextval('card.entity_index_id_seq'::regclass);


--
-- Name: garmin_ingest_log id; Type: DEFAULT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.garmin_ingest_log ALTER COLUMN id SET DEFAULT nextval('health.garmin_ingest_log_id_seq'::regclass);


--
-- Name: monthly_trend_log id; Type: DEFAULT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.monthly_trend_log ALTER COLUMN id SET DEFAULT nextval('health.monthly_trend_log_id_seq'::regclass);


--
-- Name: agent_step agent_step_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.agent_step
    ADD CONSTRAINT agent_step_pkey PRIMARY KEY (id);


--
-- Name: anomaly_detector_state anomaly_detector_state_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.anomaly_detector_state
    ADD CONSTRAINT anomaly_detector_state_pkey PRIMARY KEY (id);


--
-- Name: anomaly_disposition anomaly_disposition_metric_key_date_key; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.anomaly_disposition
    ADD CONSTRAINT anomaly_disposition_metric_key_date_key UNIQUE (metric_key, date);


--
-- Name: anomaly_disposition anomaly_disposition_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.anomaly_disposition
    ADD CONSTRAINT anomaly_disposition_pkey PRIMARY KEY (id);


--
-- Name: chat_person chat_person_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.chat_person
    ADD CONSTRAINT chat_person_pkey PRIMARY KEY (chat_id);


--
-- Name: consilium_report consilium_report_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.consilium_report
    ADD CONSTRAINT consilium_report_pkey PRIMARY KEY (id);


--
-- Name: dialog_turn dialog_turn_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.dialog_turn
    ADD CONSTRAINT dialog_turn_pkey PRIMARY KEY (id);


--
-- Name: disagreement disagreement_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.disagreement
    ADD CONSTRAINT disagreement_pkey PRIMARY KEY (id);


--
-- Name: doctor_pending_turn doctor_pending_turn_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.doctor_pending_turn
    ADD CONSTRAINT doctor_pending_turn_pkey PRIMARY KEY (id);


--
-- Name: entity_index entity_index_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.entity_index
    ADD CONSTRAINT entity_index_pkey PRIMARY KEY (id);


--
-- Name: episode episode_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.episode
    ADD CONSTRAINT episode_pkey PRIMARY KEY (id);


--
-- Name: err_dedup_state err_dedup_state_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.err_dedup_state
    ADD CONSTRAINT err_dedup_state_pkey PRIMARY KEY (key);


--
-- Name: expectation expectation_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.expectation
    ADD CONSTRAINT expectation_pkey PRIMARY KEY (id);


--
-- Name: extraction extraction_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.extraction
    ADD CONSTRAINT extraction_pkey PRIMARY KEY (id);


--
-- Name: fact fact_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.fact
    ADD CONSTRAINT fact_pkey PRIMARY KEY (id);


--
-- Name: host_metrics host_metrics_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.host_metrics
    ADD CONSTRAINT host_metrics_pkey PRIMARY KEY (ts);


--
-- Name: intervention intervention_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.intervention
    ADD CONSTRAINT intervention_pkey PRIMARY KEY (id);


--
-- Name: issue_log issue_log_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.issue_log
    ADD CONSTRAINT issue_log_pkey PRIMARY KEY (natural_key);


--
-- Name: journal journal_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.journal
    ADD CONSTRAINT journal_pkey PRIMARY KEY (j_id);


--
-- Name: lab_result lab_result_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.lab_result
    ADD CONSTRAINT lab_result_pkey PRIMARY KEY (id);


--
-- Name: llm_usage llm_usage_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.llm_usage
    ADD CONSTRAINT llm_usage_pkey PRIMARY KEY (id);


--
-- Name: memory_note memory_note_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.memory_note
    ADD CONSTRAINT memory_note_pkey PRIMARY KEY (id);


--
-- Name: metric_coverage metric_coverage_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.metric_coverage
    ADD CONSTRAINT metric_coverage_pkey PRIMARY KEY (metric_key);


--
-- Name: notify_log notify_log_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.notify_log
    ADD CONSTRAINT notify_log_pkey PRIMARY KEY (id);


--
-- Name: opinion opinion_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.opinion
    ADD CONSTRAINT opinion_pkey PRIMARY KEY (id);


--
-- Name: problem problem_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.problem
    ADD CONSTRAINT problem_pkey PRIMARY KEY (id);


--
-- Name: publication publication_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.publication
    ADD CONSTRAINT publication_pkey PRIMARY KEY (id);


--
-- Name: recommendation recommendation_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.recommendation
    ADD CONSTRAINT recommendation_pkey PRIMARY KEY (id);


--
-- Name: recommendation_verdict recommendation_verdict_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.recommendation_verdict
    ADD CONSTRAINT recommendation_verdict_pkey PRIMARY KEY (id);


--
-- Name: research_topic research_topic_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.research_topic
    ADD CONSTRAINT research_topic_pkey PRIMARY KEY (id);


--
-- Name: research_topic research_topic_topic_key_key; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.research_topic
    ADD CONSTRAINT research_topic_topic_key_key UNIQUE (topic_key);


--
-- Name: rf_event rf_event_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.rf_event
    ADD CONSTRAINT rf_event_pkey PRIMARY KEY (id);


--
-- Name: rf_session rf_session_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.rf_session
    ADD CONSTRAINT rf_session_pkey PRIMARY KEY (id);


--
-- Name: scheduler_run_log scheduler_run_log_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.scheduler_run_log
    ADD CONSTRAINT scheduler_run_log_pkey PRIMARY KEY (name);


--
-- Name: source_message source_message_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.source_message
    ADD CONSTRAINT source_message_pkey PRIMARY KEY (id);


--
-- Name: telegram_poll_state telegram_poll_state_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.telegram_poll_state
    ADD CONSTRAINT telegram_poll_state_pkey PRIMARY KEY (id);


--
-- Name: visit visit_pkey; Type: CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.visit
    ADD CONSTRAINT visit_pkey PRIMARY KEY (id);


--
-- Name: _migration_log _migration_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health._migration_log
    ADD CONSTRAINT _migration_log_pkey PRIMARY KEY (id);


--
-- Name: action_log action_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.action_log
    ADD CONSTRAINT action_log_pkey PRIMARY KEY ("Action_ID");


--
-- Name: anamnesis anamnesis_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.anamnesis
    ADD CONSTRAINT anamnesis_pkey PRIMARY KEY ("Q_ID");


--
-- Name: anomaly_alerted anomaly_alerted_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.anomaly_alerted
    ADD CONSTRAINT anomaly_alerted_pkey PRIMARY KEY (key);


--
-- Name: anomaly_log anomaly_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.anomaly_log
    ADD CONSTRAINT anomaly_log_pkey PRIMARY KEY (date);


--
-- Name: backup_alert_state backup_alert_state_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.backup_alert_state
    ADD CONSTRAINT backup_alert_state_pkey PRIMARY KEY (id);


--
-- Name: daily_trends daily_trends_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.daily_trends
    ADD CONSTRAINT daily_trends_pkey PRIMARY KEY ("Дата");


--
-- Name: day_sum day_sum_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.day_sum
    ADD CONSTRAINT day_sum_pkey PRIMARY KEY ("Date");


--
-- Name: digest_log digest_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.digest_log
    ADD CONSTRAINT digest_log_pkey PRIMARY KEY (period_start);


--
-- Name: doctor_notes doctor_notes_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.doctor_notes
    ADD CONSTRAINT doctor_notes_pkey PRIMARY KEY (id);


--
-- Name: garmin_ingest_log garmin_ingest_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.garmin_ingest_log
    ADD CONSTRAINT garmin_ingest_log_pkey PRIMARY KEY (id);


--
-- Name: gate_state gate_state_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.gate_state
    ADD CONSTRAINT gate_state_pkey PRIMARY KEY (id);


--
-- Name: investigations investigations_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.investigations
    ADD CONSTRAINT investigations_pkey PRIMARY KEY (inv_id);


--
-- Name: lab_plan lab_plan_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.lab_plan
    ADD CONSTRAINT lab_plan_pkey PRIMARY KEY ("Plan_ID");


--
-- Name: live_steps_today live_steps_today_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.live_steps_today
    ADD CONSTRAINT live_steps_today_pkey PRIMARY KEY (date);


--
-- Name: markers markers_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.markers
    ADD CONSTRAINT markers_pkey PRIMARY KEY ("Marker_ID");


--
-- Name: meals meals_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.meals
    ADD CONSTRAINT meals_pkey PRIMARY KEY ("Entry_ID");


--
-- Name: meds meds_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.meds
    ADD CONSTRAINT meds_pkey PRIMARY KEY ("Med_ID");


--
-- Name: microclimate microclimate_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.microclimate
    ADD CONSTRAINT microclimate_pkey PRIMARY KEY ("Дата");


--
-- Name: month_sum month_sum_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.month_sum
    ADD CONSTRAINT month_sum_pkey PRIMARY KEY (month, calc_method);


--
-- Name: month_wellness_log month_wellness_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.month_wellness_log
    ADD CONSTRAINT month_wellness_log_pkey PRIMARY KEY (month, calc_method);


--
-- Name: monthly_trend_log monthly_trend_log_period_month_domain_metric_key; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.monthly_trend_log
    ADD CONSTRAINT monthly_trend_log_period_month_domain_metric_key UNIQUE (period_month, domain, metric);


--
-- Name: monthly_trend_log monthly_trend_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.monthly_trend_log
    ADD CONSTRAINT monthly_trend_log_pkey PRIMARY KEY (id);


--
-- Name: nutrient_targets nutrient_targets_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.nutrient_targets
    ADD CONSTRAINT nutrient_targets_pkey PRIMARY KEY ("Нутриент");


--
-- Name: nutrition_profile nutrition_profile_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.nutrition_profile
    ADD CONSTRAINT nutrition_profile_pkey PRIMARY KEY ("User_ID");


--
-- Name: patient_state patient_state_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.patient_state
    ADD CONSTRAINT patient_state_pkey PRIMARY KEY ("State_ID");


--
-- Name: people people_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.people
    ADD CONSTRAINT people_pkey PRIMARY KEY (id);


--
-- Name: phenoage_log phenoage_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.phenoage_log
    ADD CONSTRAINT phenoage_log_pkey PRIMARY KEY (date, formula_version);


--
-- Name: results results_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.results
    ADD CONSTRAINT results_pkey PRIMARY KEY ("Visit_ID", "Marker_ID");


--
-- Name: symptom_log symptom_log_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.symptom_log
    ADD CONSTRAINT symptom_log_pkey PRIMARY KEY (id);


--
-- Name: user_profile user_profile_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.user_profile
    ADD CONSTRAINT user_profile_pkey PRIMARY KEY ("User_ID");


--
-- Name: visits visits_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.visits
    ADD CONSTRAINT visits_pkey PRIMARY KEY ("Visit_ID");


--
-- Name: watchdog_nudged watchdog_nudged_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.watchdog_nudged
    ADD CONSTRAINT watchdog_nudged_pkey PRIMARY KEY (key);


--
-- Name: watchdog_state watchdog_state_pkey; Type: CONSTRAINT; Schema: health; Owner: -
--

ALTER TABLE ONLY health.watchdog_state
    ADD CONSTRAINT watchdog_state_pkey PRIMARY KEY (id);


--
-- Name: agent_step_turn_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX agent_step_turn_idx ON card.agent_step USING btree (turn_id);


--
-- Name: anomaly_disposition_metric_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX anomaly_disposition_metric_idx ON card.anomaly_disposition USING btree (metric_key, date DESC);


--
-- Name: anomaly_disposition_pending_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX anomaly_disposition_pending_idx ON card.anomaly_disposition USING btree (disposition, created_ts) WHERE (disposition = 'pending'::text);


--
-- Name: consilium_report_ts_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX consilium_report_ts_idx ON card.consilium_report USING btree (ts_recorded DESC);


--
-- Name: dialog_turn_chat_ts_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX dialog_turn_chat_ts_idx ON card.dialog_turn USING btree (chat_id, ts DESC);


--
-- Name: dialog_turn_update_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX dialog_turn_update_idx ON card.dialog_turn USING btree (chat_id, update_id) WHERE (update_id IS NOT NULL);


--
-- Name: disagreement_report_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX disagreement_report_idx ON card.disagreement USING btree (report_id);


--
-- Name: entity_index_lookup; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX entity_index_lookup ON card.entity_index USING btree (entity_type, entity_value);


--
-- Name: fact_device_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX fact_device_dedup ON card.fact USING btree (metric_key, ts_event) WHERE ((provenance ->> 'origin'::text) = 'device'::text);


--
-- Name: fact_nutrition_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX fact_nutrition_dedup ON card.fact USING btree (metric_key, ts_event) WHERE ((provenance ->> 'origin'::text) = 'nutrition'::text);


--
-- Name: intervention_source_ref_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX intervention_source_ref_dedup ON card.intervention USING btree (((provenance ->> 'source_ref'::text)));


--
-- Name: issue_log_open_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX issue_log_open_idx ON card.issue_log USING btree (status, severity) WHERE (status = 'open'::text);


--
-- Name: lab_result_source_ref_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX lab_result_source_ref_dedup ON card.lab_result USING btree (((provenance ->> 'source_ref'::text)));


--
-- Name: llm_usage_ts; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX llm_usage_ts ON card.llm_usage USING btree (ts DESC);


--
-- Name: notify_log_budget_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX notify_log_budget_idx ON card.notify_log USING btree (sent_date, priority, immediate);


--
-- Name: notify_log_digest_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX notify_log_digest_idx ON card.notify_log USING btree (sent_date, delivered_in_digest) WHERE (immediate = false);


--
-- Name: opinion_report_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX opinion_report_idx ON card.opinion USING btree (report_id);


--
-- Name: publication_relevant_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX publication_relevant_idx ON card.publication USING btree (relevant, shown_in_digest);


--
-- Name: publication_source_ref_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX publication_source_ref_dedup ON card.publication USING btree (((provenance ->> 'source_ref'::text)));


--
-- Name: publication_topic_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX publication_topic_idx ON card.publication USING btree (topic_key);


--
-- Name: recommendation_source_ref_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX recommendation_source_ref_dedup ON card.recommendation USING btree (((provenance ->> 'source_ref'::text)));


--
-- Name: recommendation_topic_key_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX recommendation_topic_key_idx ON card.recommendation USING btree (topic_key) WHERE (status = 'active'::text);


--
-- Name: rf_event_session_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX rf_event_session_idx ON card.rf_event USING btree (session_id);


--
-- Name: rf_session_category_status_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX rf_session_category_status_idx ON card.rf_session USING btree (category, status);


--
-- Name: source_message_hash_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX source_message_hash_idx ON card.source_message USING btree (hash);


--
-- Name: source_message_status_idx; Type: INDEX; Schema: card; Owner: -
--

CREATE INDEX source_message_status_idx ON card.source_message USING btree (status);


--
-- Name: visit_source_ref_dedup; Type: INDEX; Schema: card; Owner: -
--

CREATE UNIQUE INDEX visit_source_ref_dedup ON card.visit USING btree (((provenance ->> 'source_ref'::text)));


--
-- Name: daily_trends_date; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX daily_trends_date ON health.daily_trends USING btree ("Дата" DESC);


--
-- Name: day_sum_date; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX day_sum_date ON health.day_sum USING btree ("Date" DESC);


--
-- Name: doctor_notes_date; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX doctor_notes_date ON health.doctor_notes USING btree (note_date DESC);


--
-- Name: garmin_ingest_log_date_idx; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX garmin_ingest_log_date_idx ON health.garmin_ingest_log USING btree (date);


--
-- Name: garmin_ingest_log_status_idx; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX garmin_ingest_log_status_idx ON health.garmin_ingest_log USING btree (status) WHERE (status <> 'done'::text);


--
-- Name: investigations_status; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX investigations_status ON health.investigations USING btree (status);


--
-- Name: meals_date; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX meals_date ON health.meals USING btree ("Date" DESC);


--
-- Name: recommendations_log_date; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX recommendations_log_date ON health.recommendations_log USING btree ("Date" DESC);


--
-- Name: recommendations_log_nat_key; Type: INDEX; Schema: health; Owner: -
--

CREATE UNIQUE INDEX recommendations_log_nat_key ON health.recommendations_log USING btree (_nat_key);


--
-- Name: symptom_log_sid_ts; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX symptom_log_sid_ts ON health.symptom_log USING btree (symptom_id, ts DESC);


--
-- Name: symptom_log_ts; Type: INDEX; Schema: health; Owner: -
--

CREATE INDEX symptom_log_ts ON health.symptom_log USING btree (ts DESC);


--
-- Name: investigations investigations_touch; Type: TRIGGER; Schema: health; Owner: -
--

CREATE TRIGGER investigations_touch BEFORE UPDATE ON health.investigations FOR EACH ROW EXECUTE FUNCTION health.touch_updated_at();


--
-- Name: agent_step agent_step_turn_id_fkey; Type: FK CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.agent_step
    ADD CONSTRAINT agent_step_turn_id_fkey FOREIGN KEY (turn_id) REFERENCES card.dialog_turn(id);


--
-- Name: rf_event rf_event_session_id_fkey; Type: FK CONSTRAINT; Schema: card; Owner: -
--

ALTER TABLE ONLY card.rf_event
    ADD CONSTRAINT rf_event_session_id_fkey FOREIGN KEY (session_id) REFERENCES card.rf_session(id);


--
-- PostgreSQL database dump complete
--

\unrestrict tPiA4guwBHU416pXlLsrl8CCGvFE7JH2ONmcjZTn6kbgliDqo03mHrk7ynb0r0Z

