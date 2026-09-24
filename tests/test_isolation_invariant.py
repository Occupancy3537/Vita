"""ROADMAP 0.7 (2026-09-24): страховочный тест поверх конструктивной защиты
(_isolate_real_schema_writes в conftest.py). Основная гарантия — что
app/db.py::get_conn() это единственная точка создания соединений и её
единственный вызов psycopg.connect() перехватывается на уровне транзакции
(commit() — no-op), поэтому НИКАКОЙ SQL из теста физически не может
закоммититься в прод. Этот тест — не замена той защите, а отдельный слой:
если однажды в tests/ появится код, открывающий соединение В ОБХОД
app.db.get_conn() (прямой psycopg.connect()), конструктивная защита выше
его не увидит вообще — этот grep-тест единственный, кто это заметит."""
import pathlib
import re

TESTS_DIR = pathlib.Path(__file__).parent
_DIRECT_CONNECT = re.compile(r"psycopg2?\.connect\s*\(")


def test_no_direct_psycopg_connect_in_tests():
    """Единственная точка создания соединений — app/db.py::get_conn(). Прямой
    psycopg.connect()/psycopg2.connect() в tests/ обходил бы
    _isolate_real_schema_writes (она перехватывает именно psycopg.connect,
    но только тот вызов, что происходит ЧЕРЕЗ get_conn — см. её докстринг
    про порядок патчинга) и писал бы в боевую базу напрямую."""
    offenders = []
    for path in TESTS_DIR.glob("*.py"):
        if path.name == "test_isolation_invariant.py":
            continue  # этот же файл содержит паттерн в строке выше — не самопроверка
        text = path.read_text(encoding="utf-8")
        if _DIRECT_CONNECT.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"прямой psycopg.connect() найден в: {offenders} — "
        "используйте app.db.get_conn(), иначе _isolate_real_schema_writes не защитит"
    )
