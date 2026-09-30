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
from contextlib import contextmanager
from typing import Iterator

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
