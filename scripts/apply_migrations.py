"""Apply versioned SQL migrations with checksum-drift protection."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psycopg

MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def discover_migrations(directory: str | Path) -> list[Migration]:
    root = Path(directory)
    migrations: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise ValueError(f"Migration is empty: {path.name}")
        migrations.append(
            Migration(
                version=path.stem,
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    if not migrations:
        raise ValueError(f"No SQL migrations found in {root}.")
    prefixes = [migration.version.split("_", 1)[0] for migration in migrations]
    if len(prefixes) != len(set(prefixes)):
        raise ValueError("Migration numeric prefixes must be unique.")
    return migrations


def _ledger_exists(connection: psycopg.Connection) -> bool:
    row = connection.execute(
        "SELECT to_regclass('public.schema_migrations')"
    ).fetchone()
    return bool(row and row[0])


def validate_applied_checksum(migration: Migration, checksum: str) -> None:
    if checksum != migration.checksum:
        raise RuntimeError(
            f"Checksum drift detected for {migration.version}."
        )


def apply_migrations(
    connection: psycopg.Connection,
    migrations: Iterable[Migration],
) -> list[str]:
    applied: list[str] = []
    for migration in migrations:
        with connection.transaction():
            existing = None
            if _ledger_exists(connection):
                existing = connection.execute(
                    """
                    SELECT checksum
                    FROM schema_migrations
                    WHERE version = %s
                    """,
                    (migration.version,),
                ).fetchone()
            if existing is not None:
                validate_applied_checksum(migration, existing[0])
                continue

            connection.execute(migration.sql, prepare=False)
            if not _ledger_exists(connection):
                raise RuntimeError(
                    f"Migration {migration.version} did not create the "
                    "migration ledger."
                )
            connection.execute(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES (%s, %s)
                """,
                (migration.version, migration.checksum),
            )
            applied.append(migration.version)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", "").strip()
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "migrations",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    migrations = discover_migrations(args.directory)
    with psycopg.connect(
        args.database_url, prepare_threshold=None
    ) as connection:
        applied = apply_migrations(connection, migrations)
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database schema is already current.")


if __name__ == "__main__":
    main()
