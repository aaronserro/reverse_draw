"""
Storage: the whole draw is one JSON document in one table row.

- DATABASE_URL set   -> Postgres (Supabase in production)
- DATABASE_URL unset -> local SQLite file (for running on your own machine)

Writes go through `transaction()`, which locks the row so two admin clicks
can never run the same round twice.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

from . import config

log = logging.getLogger("reverse_draw.db")

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS draw_state (
    id         INT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO draw_state (id, data) VALUES (1, '{}'::jsonb) ON CONFLICT (id) DO NOTHING;
"""


class Box:
    """Mutable holder handed to the caller inside a transaction."""

    def __init__(self, data: dict) -> None:
        self.data = data


class PostgresStore:
    kind = "postgres"

    def __init__(self, url: str) -> None:
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None keeps it compatible with Supabase's poolers.
        self.pool = ConnectionPool(
            url, min_size=1, max_size=5, open=True, kwargs={"prepare_threshold": None}
        )
        with self.pool.connection() as conn:
            for stmt in SCHEMA_PG.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)

    def read(self) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT data FROM draw_state WHERE id = 1").fetchone()
            return row[0] if row else {}

    @contextmanager
    def transaction(self) -> Iterator[Box]:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT data FROM draw_state WHERE id = 1 FOR UPDATE").fetchone()
            box = Box(row[0] if row else {})
            yield box
            conn.execute(
                "UPDATE draw_state SET data = %s, updated_at = now() WHERE id = 1", (Jsonb(box.data),)
            )

    def close(self) -> None:
        self.pool.close()


class RelationalDatabase:
    """Pooled access to the normalized Supabase schema for one active draw."""

    kind = "supabase-relational"

    def __init__(
        self,
        url: str,
        active_draw_id: str | uuid.UUID,
        *,
        required_schema_version: str = "009_marketplace_concurrency",
    ) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.active_draw_id = uuid.UUID(str(active_draw_id))
        self.required_schema_version = required_schema_version

        def configure_connection(connection) -> None:
            connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{config.DATABASE_STATEMENT_TIMEOUT_MS}ms",),
            )
            connection.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{config.DATABASE_LOCK_TIMEOUT_MS}ms",),
            )
            connection.execute(
                "SELECT set_config("
                "'idle_in_transaction_session_timeout', %s, false)",
                (f"{config.DATABASE_IDLE_TRANSACTION_TIMEOUT_MS}ms",),
            )
            connection.commit()

        self.pool = ConnectionPool(
            url,
            min_size=config.DATABASE_POOL_MIN,
            max_size=config.DATABASE_POOL_MAX,
            timeout=config.DATABASE_POOL_TIMEOUT_SECONDS,
            open=True,
            configure=configure_connection,
            kwargs={
                "prepare_threshold": None,
                "row_factory": dict_row,
            },
        )
        try:
            # Remote poolers may need longer to establish the first TLS
            # connection than an already-warm request may wait for checkout.
            self.pool.wait(
                timeout=config.DATABASE_POOL_STARTUP_TIMEOUT_SECONDS
            )
            self.verify_ready()
        except BaseException:
            self.pool.close()
            raise

    def verify_ready(self) -> None:
        with self.pool.connection() as conn:
            schema = conn.execute(
                """
                SELECT checksum
                FROM schema_migrations
                WHERE version = %s
                """,
                (self.required_schema_version,),
            ).fetchone()
            if schema is None:
                raise RuntimeError(
                    "The relational database schema is not current; "
                    f"missing {self.required_schema_version}."
                )
            draw = conn.execute(
                "SELECT id FROM draws WHERE id = %s",
                (self.active_draw_id,),
            ).fetchone()
            if draw is None:
                raise RuntimeError(
                    f"ACTIVE_DRAW_ID {self.active_draw_id} does not exist."
                )

    def sync_unstarted_schedule(self, schedule: dict) -> bool:
        """Apply configured stages when no round is currently completed."""
        desired = [
            (index, label, kind, prize, survivor_target)
            for index, (label, kind, prize, survivor_target) in enumerate(
                zip(
                    schedule["labels"],
                    schedule["kinds"],
                    schedule["prizes"],
                    schedule["survivors"],
                    strict=True,
                ),
                start=1,
            )
        ]
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                "SELECT id FROM draws WHERE id = %s FOR UPDATE",
                (self.active_draw_id,),
            )
            completed = connection.execute(
                """
                SELECT count(*) FROM draw_rounds
                WHERE draw_id = %s AND status = 'completed'
                """,
                (self.active_draw_id,),
            ).fetchone()["count"]
            if completed:
                return False
            current_rows = connection.execute(
                """
                SELECT stage_number, label, kind, prize, survivor_target
                FROM draw_stages
                WHERE draw_id = %s
                ORDER BY stage_number
                """,
                (self.active_draw_id,),
            ).fetchall()
            current = [
                (
                    row["stage_number"],
                    row["label"],
                    row["kind"],
                    row["prize"],
                    row["survivor_target"],
                )
                for row in current_rows
            ]
            if current == desired:
                return False
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO draw_stages (
                        draw_id, stage_number, label, kind, prize,
                        survivor_target
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (draw_id, stage_number) DO UPDATE SET
                        label = EXCLUDED.label,
                        kind = EXCLUDED.kind,
                        prize = EXCLUDED.prize,
                        survivor_target = EXCLUDED.survivor_target
                    """,
                    [
                        (self.active_draw_id, *stage)
                        for stage in desired
                    ],
                )
            extra_stage_numbers = [
                row["stage_number"]
                for row in current_rows
                if row["stage_number"] > len(desired)
            ]
            if extra_stage_numbers:
                referenced = connection.execute(
                    """
                    SELECT DISTINCT s.stage_number
                    FROM draw_stages s
                    JOIN draw_rounds r
                      ON r.stage_id = s.id AND r.draw_id = s.draw_id
                    WHERE s.draw_id = %s AND s.stage_number = ANY(%s)
                    """,
                    (self.active_draw_id, extra_stage_numbers),
                ).fetchall()
                if referenced:
                    raise RuntimeError(
                        "The configured schedule cannot remove stages that "
                        "are referenced by the draw audit history."
                    )
                connection.execute(
                    """
                    DELETE FROM draw_stages
                    WHERE draw_id = %s AND stage_number = ANY(%s)
                    """,
                    (self.active_draw_id, extra_stage_numbers),
                )
            connection.execute(
                """
                UPDATE draws
                SET completion_label = %s,
                    completion_label_plural = %s,
                    version = version + 1
                WHERE id = %s
                """,
                (
                    schedule["completion_label"],
                    schedule["completion_label_plural"],
                    self.active_draw_id,
                ),
            )
            return True

    @contextmanager
    def connection(self):
        with self.pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self):
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def close(self) -> None:
        self.pool.close()


class SQLiteStore:
    kind = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        with closing(self._conn()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS draw_state (id INTEGER PRIMARY KEY, data TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute("INSERT OR IGNORE INTO draw_state (id, data) VALUES (1, '{}')")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None, timeout=10)

    def read(self) -> dict:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT data FROM draw_state WHERE id = 1").fetchone()
            return json.loads(row[0]) if row else {}

    @contextmanager
    def transaction(self) -> Iterator[Box]:
        with self.lock, closing(self._conn()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT data FROM draw_state WHERE id = 1").fetchone()
                box = Box(json.loads(row[0]) if row else {})
                yield box
                conn.execute(
                    "UPDATE draw_state SET data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (json.dumps(box.data),),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        pass


@contextmanager
def closing(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
    finally:
        conn.close()


def make_store() -> PostgresStore | SQLiteStore:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        log.info("Using Postgres storage")
        return PostgresStore(url)
    path = os.getenv("SQLITE_PATH", "reverse_draw.db")
    log.warning("DATABASE_URL not set - using local SQLite file %s", path)
    return SQLiteStore(path)


def make_relational_database(
    url: str | None = None,
    active_draw_id: str | uuid.UUID | None = None,
) -> RelationalDatabase:
    database_url = (url or os.getenv("DATABASE_URL", "")).strip()
    draw_id = active_draw_id or os.getenv("ACTIVE_DRAW_ID", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for relational storage.")
    if not draw_id:
        raise RuntimeError(
            "ACTIVE_DRAW_ID is required for relational storage."
        )
    return RelationalDatabase(database_url, draw_id)
