-- Vita v2, этап 2 («Проверки») — решения по вопросам + «когда впервые увидели»
-- для детективных находок (у них самих нет персистентной строки — findings
-- пересчитываются заново при каждом вызове analyze_problem(), поэтому id
-- вопроса детерминированный (problem_id+фактор+лаг), а не первичный ключ БД).

CREATE TABLE card.vita_question_decision (
    question_id text PRIMARY KEY,
    source text NOT NULL,              -- 'detective' | 'disagreement'
    decision text NOT NULL,            -- 'checked' | 'declined'
    reason text,
    created_rec_id text,
    ts_recorded timestamp with time zone NOT NULL DEFAULT now()
);

-- Только для source='detective' (у 'disagreement' уже есть card.disagreement.ts_recorded) —
-- используется, чтобы держать открытие детектива в слоте «Решить» на главной
-- ровно 3 дня (Часть 2.5 тикета), дальше оно живёт только во вкладке «Вопросы».
CREATE TABLE card.vita_question_seen (
    question_id text PRIMARY KEY,
    first_seen_ts timestamp with time zone NOT NULL DEFAULT now()
);
