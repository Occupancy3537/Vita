"""Тонкий раннер миграций схемы (тикет «хвост», 2026-09-26, Часть 1). Журнал
применённых — public._migration_log (version, name, applied_at). Не часть
runtime-приложения: не запускается автоматически при старте контейнера — DDL
на card/health исторически всегда было отдельным осознанным действием
(см. RUNBOOK.md «Написать новую миграцию схемы БД» — там же про роль и DSN,
которые нужны для up/down: card_service обычно НЕ владелец схем).

Запуск:
    python3 scripts/migrate.py status [--dsn ...]
    python3 scripts/migrate.py up     [--dsn ...]
    python3 scripts/migrate.py down VERSION --i-understand-this-is-destructive [--dsn ...]

DSN по умолчанию собирается из CARD_PG_* (как app/db.py), можно переопределить
--dsn напрямую (postgresql://user:pass@host:port/db) — обычно так и нужно
для up/down, см. RUNBOOK.

Применение — через psql (subprocess), не psycopg: миграции могут содержать
psql-метакоманды (\\restrict/\\unrestrict — pg_dump 17), dollar-quoted тела
функций/триггеров — psql разбирает это надёжнее, чем execute() одной строкой.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_NAME_RX = re.compile(r"^(\d{4})_([a-zA-Z0-9_]+)\.sql$")


def default_dsn() -> str:
    host = os.environ.get("CARD_PG_HOST", "127.0.0.1")
    port = os.environ.get("CARD_PG_PORT", "5432")
    user = os.environ.get("CARD_PG_USER", "card_service")
    password = os.environ.get("CARD_PG_PASSWORD", "")
    database = os.environ.get("CARD_PG_DATABASE", "health")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def list_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    out = []
    for p in sorted(migrations_dir.glob("*.sql")):
        if p.name.endswith(".down.sql"):
            continue
        m = _NAME_RX.match(p.name)
        if not m:
            continue
        out.append((int(m.group(1)), m.group(2), p))
    return sorted(out, key=lambda t: t[0])


def _psql_base_cmd() -> list[str]:
    """Обычно просто ["psql"] — но на этом VPS psql-клиент стоит только
    ВНУТРИ контейнера pg, не на хосте (сам card-service — Python-образ без
    postgres-client). MIGRATE_PSQL_CMD позволяет обернуть вызов, например
    "docker exec -i pg psql" — без этого up/down/тест на воспроизводимость
    схемы просто негде было бы выполнить с этого хоста."""
    override = os.environ.get("MIGRATE_PSQL_CMD")
    return override.split() if override else ["psql"]


def _psql(dsn: str, *args) -> subprocess.CompletedProcess:
    cmd = [*_psql_base_cmd(), dsn, "-v", "ON_ERROR_STOP=1", *args]
    return subprocess.run(cmd, text=True, capture_output=True)


def _psql_file(dsn: str, path: Path) -> subprocess.CompletedProcess:
    """Через stdin (`-f -`), не `-f <путь>`: MIGRATE_PSQL_CMD может оборачивать
    вызов в `docker exec` (см. _psql_base_cmd) — путь к файлу миграции на
    хосте тогда не существует ВНУТРИ контейнера, а вот stdin прокидывается
    всегда одинаково, независимо от обёртки."""
    cmd = [*_psql_base_cmd(), dsn, "-v", "ON_ERROR_STOP=1", "-f", "-"]
    return subprocess.run(cmd, input=path.read_text(encoding="utf-8"), text=True, capture_output=True)


def ensure_log_table(dsn: str) -> None:
    r = _psql(dsn, "-c",
               "CREATE TABLE IF NOT EXISTS public._migration_log "
               "(version integer PRIMARY KEY, name text NOT NULL, "
               "applied_at timestamptz NOT NULL DEFAULT now())")
    if r.returncode != 0:
        raise RuntimeError(f"не удалось создать public._migration_log: {r.stderr.strip()}")


def applied_versions(dsn: str) -> list[int]:
    r = _psql(dsn, "-t", "-A", "-c", "SELECT version FROM public._migration_log ORDER BY version")
    if r.returncode != 0:
        raise RuntimeError(f"не удалось прочитать public._migration_log: {r.stderr.strip()}")
    return [int(x) for x in r.stdout.split() if x.strip()]


def current_database(dsn: str) -> str:
    r = _psql(dsn, "-t", "-A", "-c", "SELECT current_database()")
    if r.returncode != 0:
        raise RuntimeError(f"не удалось определить current_database(): {r.stderr.strip()}")
    return r.stdout.strip()


def status(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> str:
    ensure_log_table(dsn)
    applied = set(applied_versions(dsn))
    lines = []
    for version, name, _ in list_migrations(migrations_dir):
        mark = "[x]" if version in applied else "[ ]"
        lines.append(f"{mark} {version:04d}_{name}")
    return "\n".join(lines)


def stamp(dsn: str, version: int, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """Отмечает миграцию применённой БЕЗ выполнения её SQL — только для
    0001_baseline на боевой базе: схема там уже физически существует (снимок
    pg_dump СНЯТ с неё же), выполнить 0001 ещё раз значило бы CREATE TABLE по
    таблицам, которые уже есть, и упасть. Дальше (0002+) — обычный up()."""
    ensure_log_table(dsn)
    match = next(((v, n) for v, n, _ in list_migrations(migrations_dir) if v == version), None)
    if match is None:
        raise RuntimeError(f"миграция с версией {version:04d} не найдена на диске")
    v, name = match
    if v in applied_versions(dsn):
        raise RuntimeError(f"миграция {v:04d}_{name} уже отмечена применённой")
    r = _psql(dsn, "-c", f"INSERT INTO public._migration_log (version, name) VALUES ({v}, '{name}')")
    if r.returncode != 0:
        raise RuntimeError(f"не удалось отметить {v:04d}_{name}: {r.stderr.strip()}")


def up(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Применяет все неприменённые миграции по порядку. Честная ошибка, если
    следующая неприменённая на диске — не (последняя применённая + 1): нельзя
    перепрыгнуть номер (Часть 1.2 тикета)."""
    ensure_log_table(dsn)
    applied = applied_versions(dsn)
    last = max(applied) if applied else 0
    applied_set = set(applied)
    newly_applied = []
    for version, name, path in list_migrations(migrations_dir):
        if version in applied_set:
            continue
        if version != last + 1:
            raise RuntimeError(
                f"нельзя перепрыгнуть миграцию: следующая ожидаемая версия — {last + 1:04d}, "
                f"а первая неприменённая на диске — {version:04d}_{name}. "
                f"Применяются строго по порядку."
            )
        r = _psql_file(dsn, path)
        if r.returncode != 0:
            raise RuntimeError(f"миграция {version:04d}_{name} упала:\n{r.stderr.strip()}")
        r2 = _psql(dsn, "-c",
                   f"INSERT INTO public._migration_log (version, name) VALUES ({version}, '{name}')")
        if r2.returncode != 0:
            raise RuntimeError(
                f"миграция {version:04d}_{name} применилась к схеме, но НЕ записалась в журнал "
                f"(_migration_log) — проверь вручную, повторный запуск up может продублировать DDL: "
                f"{r2.stderr.strip()}"
            )
        newly_applied.append(version)
        last = version
    return newly_applied


def down(dsn: str, target_version: int, confirmed: bool, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """Часть 1.2: .down выполняется ТОЛЬКО на чистой тестовой базе — здесь это
    означает "не та база, что сконфигурирована как боевая" (CARD_PG_DATABASE),
    сравнение по имени базы, к которой реально подключились (--dsn может
    указывать куда угодно, включая прод по ошибке — это и ловим)."""
    if not confirmed:
        raise RuntimeError("down требует --i-understand-this-is-destructive (DROP CASCADE, необратимо)")
    prod_db_name = os.environ.get("CARD_PG_DATABASE", "health")
    actual_db_name = current_database(dsn)
    if actual_db_name == prod_db_name:
        raise RuntimeError(
            f"отказ: down нацелен на базу '{actual_db_name}', которая совпадает с боевой "
            f"(CARD_PG_DATABASE={prod_db_name}). down выполняется только на отдельной "
            f"проверочной базе — укажи --dsn с другим именем базы."
        )
    match = None
    for version, name, path in list_migrations(migrations_dir):
        if version == target_version:
            match = (version, name, path)
            break
    if match is None:
        raise RuntimeError(f"миграция с версией {target_version:04d} не найдена на диске")
    version, name, path = match
    down_path = path.with_name(path.name[:-4] + ".down.sql")
    if not down_path.exists():
        raise RuntimeError(f"нет файла отката для {version:04d}_{name}: {down_path.name}")
    r = _psql_file(dsn, down_path)
    if r.returncode != 0:
        raise RuntimeError(f"откат {version:04d}_{name} упал:\n{r.stderr.strip()}")
    r2 = _psql(dsn, "-c", f"DELETE FROM public._migration_log WHERE version = {version}")
    if r2.returncode != 0:
        raise RuntimeError(f"откат применился, но не убрался из журнала: {r2.stderr.strip()}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "up", "down", "stamp"])
    parser.add_argument("version", nargs="?", type=int, help="для down/stamp: версия миграции")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--i-understand-this-is-destructive", action="store_true", dest="confirmed")
    args = parser.parse_args(argv)
    dsn = args.dsn or default_dsn()

    try:
        if args.command == "status":
            print(status(dsn))
        elif args.command == "up":
            newly = up(dsn)
            print(f"применено: {len(newly)}" + (f" ({newly})" if newly else " (уже всё применено)"))
        elif args.command == "down":
            if args.version is None:
                parser.error("down требует номер версии")
            down(dsn, args.version, args.confirmed)
            print(f"откачено: {args.version:04d}")
        elif args.command == "stamp":
            if args.version is None:
                parser.error("stamp требует номер версии")
            stamp(dsn, args.version)
            print(f"отмечено применённым (без выполнения SQL): {args.version:04d}")
    except RuntimeError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
