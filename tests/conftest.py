"""Тестовое окружение: всегда card_test-схема, никогда не прод card. Секреты — из
.env.test (гитигнорится), см. .env.test.example.

2026-09-24 (ROADMAP 0.7/0.2, тикет «тестовая среда должна говорить правду»):
до этого захода 16 тестовых файлов писали НАПРЯМУЮ в боевые `health.*` (14
файлов) и в боевые синглтоны `card.gate_state`... стоп, поправка — `health.
gate_state`/`health.backup_alert_state` и `card.err_dedup_state`/`card.
anomaly_detector_state` (2 файла) — производственный код в этих модулях
хардкодит буквальную схему (`health.foo`/`card.foo`) вместо параметризованного
`{schema()}.foo`, поэтому `CARD_PG_SCHEMA=card_test` их не касался вообще.
14 из 16 файлов защищались "пишем тестовое — убираем за собой" (фикстура
с DELETE по заведомо фальшивому ключу), 2 синглтона (`backup_alert_state`,
`anomaly_detector_state`) — save/restore. Один синглтон, `health.gate_state`
(гейт нагрузки при активной грыже L5/S1 — safety-критичная таблица),
защиты не имел вообще: тест `test_gate_watch.py` тупо удалял боевую строку
до/после каждого теста без сохранения — тот же класс бага, что уже стоил
двух инцидентов (#54 anomaly_log, #60 backup_alert_state), просто ещё не
успел здесь выстрелить (проверено: сегодняшние два прогона pytest стёрли
строку, живой планировщик `gate_watch` (тик каждые 15 мин) успел
переписать её правильным значением до того, как случился настоящий
переход гейта — но полагаться на удачное совпадение по времени нельзя).

Решение — не чинить каждый файл по отдельности (это будет возвращаться на
каждом новом пишущем пути, ровно как предсказало внешнее ревью про dual-canon),
а сделать прод-схему НЕДОСТИЖИМОЙ ИЗ ТЕСТОВ ПО КОНСТРУКЦИИ, а не по
договорённости "не забудь удалить за собой": см. `_NoCommitConnection`
и фикстуру `_isolate_real_schema_writes` ниже. Все 16 файлов теперь просто
запрашивают эту фикстуру (`pytestmark = pytest.mark.usefixtures(...)`) —
старые ручные DELETE/save-restore фикстуры удалены как избыточные."""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.test"))
os.environ["CARD_PG_SCHEMA"] = "card_test"  # жёстко, не полагаемся на .env.test
# 2026-09-24: было "card_test" — но visits/results/symptom_log/doctor_notes/
# investigations/lab_plan/people НЕ являются частью объектной модели card.* —
# это двойники health.* (регистратор/commit.py/timeutil.py читают их через
# REGISTRAR_HEALTH_SCHEMA), и их присутствие в card_test делало сравнение
# "card == card_test" (test_schema_parity) в принципе невозможным — внешнее
# ревью 24.09 нашло это как "схемы разошлись", хотя на самом деле это не
# дрейф, а смешение двух разных ролей в одной схеме. Отдельная схема
# health_test — как физическая метка "это не card.*, не сравнивать".
os.environ["REGISTRAR_HEALTH_SCHEMA"] = "health_test"
# T3 (2026-09-23): кеш чтения зоны в timeutil — в тестах выключен, иначе
# смена зоны в одном тесте протекала бы в соседние (TTL 60 с).
os.environ["TIMEUTIL_TZ_CACHE_SECONDS"] = "0"

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app import db as db_module  # noqa: E402
from app.db import get_conn, schema  # noqa: E402

HEALTH_TEST_SCHEMA = "health_test"

with get_conn() as _conn, _conn.cursor() as _cur:
    # Схема создана один раз вручную (card_service не имеет CREATE на уровне
    # БД, тот же принцип, что и у card_test — см. AGENT_SYNC про роли):
    # CREATE SCHEMA health_test AUTHORIZATION n8n; GRANT USAGE, CREATE ON
    # SCHEMA health_test TO card_service;

    # ---- health_test: двойники health.* (НЕ часть объектной модели card.*, ----
    # ---- поэтому не в card_test и не участвуют в test_schema_parity) ----------
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.visits (
          "Visit_ID" text PRIMARY KEY, "Date" text, "Age_at_Visit" text,
          "Lab_Name" text, "Notes" text, _synced_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.results (
          "Visit_ID" text, "Marker_ID" text, "Value" text, "Original_Unit" text,
          "Lab_Min" text, "Lab_Max" text, _synced_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY ("Visit_ID", "Marker_ID"))
    """)
    # 2026-09-21 (#38/#47, аудит ZCode): двойники для app/doctor/commit.py —
    # DDL упрощён относительно прод-схемы (без identity/trigger updated_at) —
    # ни один тест на эти детали не полагается, тот же принцип, что у visits/results.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.symptom_log (
          id bigserial PRIMARY KEY, symptom_id text NOT NULL, ts timestamptz NOT NULL,
          symptom text, system text, severity text, status text, change text,
          domain text, context text, hypothesis text, notes text,
          source text NOT NULL DEFAULT 'AI-доктор', created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.doctor_notes (
          id bigserial PRIMARY KEY, note_date date, category text, note text,
          trigger text, plan text, doctor text, source text NOT NULL DEFAULT 'AI-доктор',
          created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.investigations (
          inv_id text PRIMARY KEY, opened date, trigger text, trigger_detail text,
          hypothesis text, status text, findings text, questions_pending text,
          labs_suggested text, doctor_brief text, referral text, updated date, closed date,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.lab_plan (
          "Plan_ID" text PRIMARY KEY, "Test" text, "Category" text, "Interval_Months" text,
          "Last_Done" text, "Next_Due" text, "Reason" text, "Status" text, "Source" text,
          "Notes" text, _synced_at timestamptz NOT NULL DEFAULT now())
    """)
    # app/timeutil.py читает зону человека из {HEALTH_SCHEMA}.people.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TEST_SCHEMA}.people (
          id text PRIMARY KEY, name text NOT NULL, birth_year int,
          home_tz text NOT NULL DEFAULT 'Asia/Vladivostok',
          current_tz text NOT NULL DEFAULT 'Asia/Vladivostok',
          locale text NOT NULL DEFAULT 'ru',
          created_at timestamptz NOT NULL DEFAULT now())
    """)
    _cur.execute(
        f"INSERT INTO {HEALTH_TEST_SCHEMA}.people (id, name, birth_year) VALUES ('self', 'тест', 1982) "
        "ON CONFLICT (id) DO NOTHING"
    )

    # ---- card_test: объектная модель card.* — ДОЛЖНА быть 1:1 с card ----------
    # (test_schema_parity проверяет это по table/column, а не на глаз).
    # Страница «Настройки» (2026-09-22): журнал прогонов циклов + метрики хоста.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.scheduler_run_log (
          name text PRIMARY KEY, last_ok_at timestamptz,
          last_error text, last_error_at timestamptz)
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.host_metrics (
          ts timestamptz PRIMARY KEY, load1 real, load5 real, load15 real,
          mem_used_mb int, swap_used_mb int)
    """)
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.llm_usage (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(), module text NOT NULL, model text,
          tokens_prompt int, tokens_completion int, cost_usd numeric(12, 6))
    """)
    # 2026-09-24: единственная реальная недостача card_test относительно card
    # на момент внешнего ревью — person_id-привязка чата, `card.chat_person`.
    _cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema()}.chat_person (
          chat_id text PRIMARY KEY, person_id text NOT NULL,
          added_at timestamptz NOT NULL DEFAULT now())
    """)
    _conn.commit()


# Двойники health.* — своя схема, свой список очистки, НЕ участвуют
# в сравнении card/card_test (test_schema_parity).
HEALTH_TEST_TABLES_TO_CLEAN = [
    "visits", "results", "symptom_log", "doctor_notes", "investigations", "lab_plan",
]

# Объектная модель card.* — должна быть 1:1 с боевой card (test_schema_parity).
TABLES_TO_CLEAN = [
    "chat_person", "source_message", "extraction", "fact", "episode", "problem", "intervention",
    "opinion", "disagreement", "recommendation", "expectation", "recommendation_verdict",
    "visit", "lab_result", "memory_note", "journal", "entity_index", "metric_coverage",
    "rf_event", "rf_session", "dialog_turn", "agent_step",
    "scheduler_run_log", "host_metrics",  # статус-страница (run_log/host_metrics)
    "llm_usage",  # учёт стоимости LLM вне доктора
    "issue_log",  # бэклог продуктовых находок (2026-09-23, Шаг 1 «петли самоулучшения»)
]


@pytest.fixture(autouse=True)
def clean_all_tables():
    """Пустые таблицы перед каждым тестом — тесты не должны зависеть друг от друга.
    ВСЕГДА реальный commit (не через _isolate_real_schema_writes — см. её
    докстринг ниже про порядок фикстур), потому что card_test/health_test —
    не боевые схемы, чистить их взаправду безопасно и нужно."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(schema() + '.' + t for t in TABLES_TO_CLEAN)}")
            cur.execute(f"TRUNCATE TABLE {', '.join(HEALTH_TEST_SCHEMA + '.' + t for t in HEALTH_TEST_TABLES_TO_CLEAN)}")
        conn.commit()
    yield


# =============================================================================
# Изоляция прямых обращений к БОЕВЫМ health.*/card.* (в обход schema()) —
# ROADMAP 0.7/0.2, 2026-09-24.
#
# 16 тестовых файлов исполняют буквальный SQL вида "health.daily_trends" или
# "card.err_dedup_state" (не через параметризуемый {schema()}) — либо потому
# что production-код в этих модулях сам хардкодит схему (gate_watch.py,
# backup_alert.py, anomaly_detector.py::mark_daily_check_ran,
# err_dedup.py, small_webhooks.py и т.д.), либо потому что health.* в этом
# проекте вообще не имеет схемы-переключателя (сознательное решение — см.
# CLAUDE.md, "health — реальная прод-схема, не card_test"). Раньше это
# закрывалось по школе "пишем тестовое — убираем за собой" (DELETE по
# заведомо фальшивому TEST_-ключу) — работает, пока никто не забудет
# написать cleanup правильно. Два синглтона (health.backup_alert_state,
# card.anomaly_detector_state) уже потребовали save/restore вместо DELETE
# (после инцидентов #54/#60); третий синглтон (health.gate_state,
# safety-критичный гейт нагрузки) такой защиты не имел вовсе.
#
# Вместо повторения этой игры на каждом новом файле — соединение с боевой
# базой физически не может закоммитить ничего, что бы тест ни исполнил.
class _NoCommitConnection:
    """Прокси над РЕАЛЬНЫМ psycopg.Connection (та же роль/грант card_service,
    что и в проде — важно для test_doctor_anamnesis.py: у роли нет DELETE на
    health.anamnesis, и это ровно то, что тест проверяет живьём; открывая
    настоящее соединение теми же credentials, а не отдельной ролью, это
    свойство переживает переезд на изоляцию без изменений). Единственное
    отличие от настоящего соединения: commit() ничего не делает. Курсор
    внутри одного теста видит свои же незакоммиченные записи как обычно —
    это одна и та же (реальная) транзакция на всё время теста, поэтому
    смысл тестов не меняется, меняется только то, доезжает ли что-то до
    диска. __exit__ намеренно не коммитит и не закрывает реальное
    соединение — оно живёт до конца теста, что бы ни делал очередной
    `with get_conn() as conn:` внутри теста или внутри тестируемого кода."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def commit(self):
        pass  # намеренно: настоящий COMMIT запрещён — в этом весь смысл

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


@pytest.fixture
def _isolate_real_schema_writes(clean_all_tables, monkeypatch):
    """Единственная точка создания соединений в проде — app/db.py::get_conn()
    (единственный вызов psycopg-connect во всём app/, проверено grep —
    см. test_no_direct_psycopg_connect_in_tests). Патчим ИМЕННО
    `psycopg.connect`, а не `app.db.get_conn`: все ~14 модулей делают
    `from app.db import get_conn` (имя связывается в ИХ собственном
    пространстве имён при импорте) — патч `app.db.get_conn` их бы не
    затронул. `psycopg.connect` — это последний вызов внутри самого
    get_conn(), резолвится по атрибуту в момент вызова, поэтому виден
    отовсюду, независимо от того, кто и как импортировал get_conn.

    Зависимость от `clean_all_tables` — не для очистки, а для ПОРЯДКА:
    её TRUNCATE обязан закоммититься по-настоящему ДО того, как мы
    патчим psycopg.connect, иначе TRUNCATE тоже стал бы фиктивным
    и card_test/health_test не чистились бы между тестами."""
    real_conn = db_module.get_conn()  # настоящее соединение, ДО патча
    proxy = _NoCommitConnection(real_conn)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: proxy)
    try:
        yield
    finally:
        real_conn.rollback()
        real_conn.close()
