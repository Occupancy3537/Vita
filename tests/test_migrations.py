"""Тикет «хвост» (2026-09-26), Часть 1 — scripts/migrate.py. Юниты на чистую
логику (список миграций, честная ошибка при пропуске номера, отказ down на
проде) через monkeypatch _psql/_psql_file — без реальной БД. Отдельно —
интеграционный тест на воспроизводимость схемы (Часть 1.3 приёмки): требует
MIGRATIONS_ADMIN_PASSWORD (роль с CREATEDB, см. RUNBOOK.md) — пропускается,
если не задан, не ломает обычный прогон тестов."""
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from scripts import migrate

CARD_SERVICE = Path(__file__).resolve().parent.parent


def _write_migration(tmp_path, version, name, sql="SELECT 1;", down_sql=None):
    (tmp_path / f"{version:04d}_{name}.sql").write_text(sql, encoding="utf-8")
    if down_sql is not None:
        (tmp_path / f"{version:04d}_{name}.down.sql").write_text(down_sql, encoding="utf-8")


# --- list_migrations ---------------------------------------------------------

def test_list_migrations_sorted_by_number(tmp_path):
    _write_migration(tmp_path, 2, "second")
    _write_migration(tmp_path, 1, "first")
    out = migrate.list_migrations(tmp_path)
    assert [v for v, _, _ in out] == [1, 2]


def test_list_migrations_ignores_down_files(tmp_path):
    _write_migration(tmp_path, 1, "first", down_sql="DROP TABLE x;")
    out = migrate.list_migrations(tmp_path)
    assert len(out) == 1


def test_list_migrations_ignores_files_with_bad_names(tmp_path):
    (tmp_path / "not_a_migration.sql").write_text("SELECT 1;")
    (tmp_path / "readme.txt").write_text("hi")
    _write_migration(tmp_path, 1, "first")
    out = migrate.list_migrations(tmp_path)
    assert len(out) == 1


def test_real_migrations_dir_has_the_baseline():
    """0001_baseline.sql реально существует в репозитории (Часть 1.1)."""
    out = migrate.list_migrations(migrate.MIGRATIONS_DIR)
    assert out and out[0] == (1, "baseline", migrate.MIGRATIONS_DIR / "0001_baseline.sql")
    assert (migrate.MIGRATIONS_DIR / "0001_baseline.down.sql").exists()


# --- up(): гоняем без реальной БД, подменяя _psql/_psql_file -----------------

class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_up_applies_in_order_and_records_log(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first")
    _write_migration(tmp_path, 2, "second")
    calls = []
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [])
    monkeypatch.setattr(migrate, "_psql_file", lambda dsn, path: calls.append(("file", path.name)) or _FakeResult())
    monkeypatch.setattr(migrate, "_psql", lambda dsn, *a: calls.append(("log", a)) or _FakeResult())
    newly = migrate.up("fake-dsn", migrations_dir=tmp_path)
    assert newly == [1, 2]
    assert [c[1] for c in calls if c[0] == "file"] == ["0001_first.sql", "0002_second.sql"]


def test_up_skips_already_applied(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first")
    _write_migration(tmp_path, 2, "second")
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [1])
    calls = []
    monkeypatch.setattr(migrate, "_psql_file", lambda dsn, path: calls.append(path.name) or _FakeResult())
    monkeypatch.setattr(migrate, "_psql", lambda dsn, *a: _FakeResult())
    newly = migrate.up("fake-dsn", migrations_dir=tmp_path)
    assert newly == [2]
    assert calls == ["0002_second.sql"]


def test_up_refuses_to_skip_a_version_gap(tmp_path, monkeypatch):
    """Часть 1.2 — честная ошибка при попытке перепрыгнуть номер: 0001 не
    применена (журнал пуст), а на диске есть только 0002 — 0002 не может
    стать первой."""
    _write_migration(tmp_path, 2, "second")
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [])
    with pytest.raises(RuntimeError, match="перепрыгнуть"):
        migrate.up("fake-dsn", migrations_dir=tmp_path)


def test_up_refuses_gap_after_some_applied(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first")
    _write_migration(tmp_path, 3, "third")  # 0002 отсутствует на диске
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [1])
    with pytest.raises(RuntimeError, match="перепрыгнуть"):
        migrate.up("fake-dsn", migrations_dir=tmp_path)


def test_up_raises_honest_error_when_migration_sql_fails(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first")
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [])
    monkeypatch.setattr(migrate, "_psql_file", lambda dsn, path: _FakeResult(returncode=1, stderr="syntax error"))
    with pytest.raises(RuntimeError, match="упала"):
        migrate.up("fake-dsn", migrations_dir=tmp_path)


# --- down(): отказ на "боевой" базе, только на другой ------------------------

def test_down_refuses_without_confirmation_flag(tmp_path):
    with pytest.raises(RuntimeError, match="i-understand"):
        migrate.down("fake-dsn", 1, confirmed=False, migrations_dir=tmp_path)


def test_down_refuses_when_database_matches_prod_name(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first", down_sql="DROP TABLE x;")
    monkeypatch.setenv("CARD_PG_DATABASE", "health")
    monkeypatch.setattr(migrate, "current_database", lambda dsn: "health")
    with pytest.raises(RuntimeError, match="боевой"):
        migrate.down("fake-dsn", 1, confirmed=True, migrations_dir=tmp_path)


def test_down_proceeds_on_a_differently_named_database(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first", down_sql="DROP TABLE x;")
    monkeypatch.setenv("CARD_PG_DATABASE", "health")
    monkeypatch.setattr(migrate, "current_database", lambda dsn: "card_migrate_check_scratch")
    calls = []
    monkeypatch.setattr(migrate, "_psql_file", lambda dsn, path: calls.append(path.name) or _FakeResult())
    monkeypatch.setattr(migrate, "_psql", lambda dsn, *a: _FakeResult())
    migrate.down("fake-dsn", 1, confirmed=True, migrations_dir=tmp_path)
    assert calls == ["0001_first.down.sql"]


def test_down_missing_down_file_raises_honest_error(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "first")  # без .down.sql
    monkeypatch.setattr(migrate, "current_database", lambda dsn: "scratch")
    with pytest.raises(RuntimeError, match="нет файла отката"):
        migrate.down("fake-dsn", 1, confirmed=True, migrations_dir=tmp_path)


def test_down_unknown_version_raises_honest_error(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "current_database", lambda dsn: "scratch")
    with pytest.raises(RuntimeError, match="не найдена"):
        migrate.down("fake-dsn", 99, confirmed=True, migrations_dir=tmp_path)


# --- stamp(): отмечает без выполнения SQL ------------------------------------

def test_stamp_records_without_executing_sql(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "baseline")
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [])
    executed_files = []
    monkeypatch.setattr(migrate, "_psql_file", lambda dsn, path: executed_files.append(path) or _FakeResult())
    calls = []
    monkeypatch.setattr(migrate, "_psql", lambda dsn, *a: calls.append(a) or _FakeResult())
    migrate.stamp("fake-dsn", 1, migrations_dir=tmp_path)
    assert executed_files == []  # SQL самой миграции НЕ выполнялся
    assert any("INSERT INTO public._migration_log" in str(a) for a in calls)


def test_stamp_refuses_if_already_applied(tmp_path, monkeypatch):
    _write_migration(tmp_path, 1, "baseline")
    monkeypatch.setattr(migrate, "ensure_log_table", lambda dsn: None)
    monkeypatch.setattr(migrate, "applied_versions", lambda dsn: [1])
    with pytest.raises(RuntimeError, match="уже отмечена"):
        migrate.stamp("fake-dsn", 1, migrations_dir=tmp_path)


# =====================================================================
# Воспроизводимость схемы (Часть 1.3 приёмки) — реальная скретч-БД.
# Пропускается без MIGRATIONS_ADMIN_PASSWORD (роль с CREATEDB).
# =====================================================================

def _run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


@pytest.mark.skipif(not os.environ.get("MIGRATIONS_ADMIN_PASSWORD"),
                     reason="MIGRATIONS_ADMIN_PASSWORD не задан — нет роли с CREATEDB для этой проверки")
def test_migrations_reproduce_the_live_schema():
    psql_cmd = os.environ.get("MIGRATE_PSQL_CMD", "psql").split()
    admin_pw = os.environ["MIGRATIONS_ADMIN_PASSWORD"]
    admin_user = os.environ.get("MIGRATIONS_ADMIN_USER", "n8n")
    host = os.environ.get("CARD_PG_HOST", "127.0.0.1")
    port = os.environ.get("CARD_PG_PORT", "5432")
    admin_root_dsn = f"postgresql://{admin_user}:{admin_pw}@{host}:{port}/postgres"
    scratch_db = f"card_migrate_check_{uuid.uuid4().hex[:10]}"
    scratch_dsn = f"postgresql://{admin_user}:{admin_pw}@{host}:{port}/{scratch_db}"

    r = _run([*psql_cmd, admin_root_dsn, "-v", "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {scratch_db}"])
    assert r.returncode == 0, r.stderr
    try:
        newly = migrate.up(scratch_dsn)
        # Не хардкодим число миграций — растёт с каждым новым тикетом, трогающим
        # схему (0002_lab_request — тикет «оптимизатор сдачи анализов», 2026-09-26).
        # Важно, что применились ВСЕ по порядку без пропуска, не конкретное число.
        expected = [v for v, _, _ in migrate.list_migrations()]
        assert newly == expected

        pgdump_cmd = os.environ.get("MIGRATE_PGDUMP_CMD", "pg_dump").split()

        def dump(dsn_db):
            d = _run([*pgdump_cmd, "-U", admin_user, "-d", dsn_db,
                      "--schema-only", "-n", "card", "-n", "health", "--no-owner", "--no-privileges"])
            assert d.returncode == 0, d.stderr
            # убираем шумные строки, не отражающие структуру схемы
            lines = [ln for ln in d.stdout.splitlines()
                     if not ln.startswith("-- Dumped ") and "\\restrict" not in ln and "\\unrestrict" not in ln]
            return "\n".join(lines)

        scratch_schema = dump(scratch_db)
        prod_db = os.environ.get("CARD_PG_DATABASE", "health")
        prod_schema = dump(prod_db)
        assert scratch_schema == prod_schema, "схема после миграций с нуля разошлась с боевой"
    finally:
        _run([*psql_cmd, admin_root_dsn, "-v", "ON_ERROR_STOP=1", "-c",
              f"DROP DATABASE IF EXISTS {scratch_db} WITH (FORCE)"])
