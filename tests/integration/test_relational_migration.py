import hashlib
import os
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import psycopg
from psycopg import errors

from app import config
from app.draw import ReverseDraw
from scripts.apply_migrations import apply_migrations, discover_migrations
from scripts.migrate_legacy_state import migrate_legacy_state
from scripts.migration_common import allocation_fingerprint
from scripts.reconcile_legacy_state import build_relational_state, reconcile

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
TIMESTAMP = "2026-01-02T03:04:05+00:00"


@contextmanager
def small_schedule():
    with patch.multiple(
        config,
        TOTAL_TICKETS=5,
        ROUND_SURVIVORS=[3, 2],
        ROUND_LABELS=["Round 1", "Prize"],
        ROUND_KINDS=["elimination", "prize"],
        ROUND_PRIZES=["", "Gift Card"],
        COMPLETION_LABEL="Winner",
        COMPLETION_LABEL_PLURAL="Winners",
        RANDOM_SEED=91,
    ):
        yield


def realistic_legacy_state():
    draw = ReverseDraw()
    draw.set_owners({1: "Alice", 2: "Alice", 3: "Bob"})
    draw.run_next_round()
    frame = pd.DataFrame(
        [
            {"ticket": 1, "name": "Alice", "email": "alice@example.com"},
            {"ticket": 2, "name": "Alice", "email": "alice@example.com"},
            {"ticket": 3, "name": "Bob", "email": "bob@example.com"},
        ]
    )
    source_json = frame.to_json(orient="split", date_format="iso")
    draw.source_dataframe = {
        "filename": "holders.csv",
        "uploaded_at": TIMESTAMP,
        "json": source_json,
        "fingerprint": hashlib.sha256(source_json.encode()).hexdigest(),
    }
    draw.allocation_source_fingerprint = allocation_fingerprint(draw.owners)
    draw.holder_credentials = {
        "alice": {
            "id": "credential-alice",
            "name": "Alice",
            "digest": "digest-alice",
            "code_scheme": "derived-v1",
            "created_at": TIMESTAMP,
        },
        "bob": {
            "id": "credential-bob",
            "name": "Bob",
            "digest": "digest-bob",
            "code_scheme": "derived-v1",
            "created_at": TIMESTAMP,
        },
    }
    draw.notification_batches = [
        {
            "id": "legacy-batch",
            "status": "completed",
            "allocation_fingerprint": draw.allocation_source_fingerprint,
            "created_at": TIMESTAMP,
            "completed_at": TIMESTAMP,
            "jobs": [
                {
                    "email": "alice@example.com",
                    "name": "Alice",
                    "status": "sent",
                    "sent_at": TIMESTAMP,
                    "provider_request_id": "provider-request",
                    "attempt_count": 1,
                    "all_tickets": [1, 2],
                    "new_tickets": [1, 2],
                }
            ],
        }
    ]
    return draw.to_dict()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parents[2]
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            apply_migrations(
                connection,
                discover_migrations(root / "migrations"),
            )

    def setUp(self):
        self.draw_ids = []
        self.migration_keys = []

    def tearDown(self):
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            if self.migration_keys:
                connection.execute(
                    """
                    DELETE FROM data_migrations
                    WHERE migration_key = ANY(%s)
                    """,
                    (self.migration_keys,),
                )
            if self.draw_ids:
                connection.execute(
                    "DELETE FROM draws WHERE id = ANY(%s)",
                    (self.draw_ids,),
                )

    def _identity(self):
        draw_id = uuid.uuid4()
        migration_key = f"integration-{uuid.uuid4()}"
        self.draw_ids.append(draw_id)
        self.migration_keys.append(migration_key)
        return draw_id, migration_key

    def test_real_backfill_reconciles_exactly_and_replays_idempotently(self):
        draw_id, migration_key = self._identity()
        with small_schedule():
            legacy = realistic_legacy_state()
            with psycopg.connect(
                TEST_DATABASE_URL, prepare_threshold=None
            ) as connection:
                first = migrate_legacy_state(
                    connection,
                    legacy,
                    {"legacy_updated_at": TIMESTAMP},
                    migration_key=migration_key,
                    target_draw_id=draw_id,
                    draw_name="Migration Integration Test",
                )
                replay = migrate_legacy_state(
                    connection,
                    legacy,
                    {"legacy_updated_at": TIMESTAMP},
                    migration_key=migration_key,
                    target_draw_id=draw_id,
                    draw_name="Migration Integration Test",
                )
                relational = build_relational_state(connection, draw_id)

            report = reconcile(legacy, relational)

        self.assertEqual(replay, first)
        self.assertTrue(report["match"], report["differences"])
        self.assertEqual(first["tickets"], 5)
        self.assertEqual(first["assigned_tickets"], 3)
        self.assertEqual(first["participants"], 2)
        self.assertEqual(first["completed_rounds"], 1)
        self.assertEqual(first["email_jobs"], 1)

    def test_database_failure_rolls_back_every_backfill_table(self):
        draw_id, migration_key = self._identity()
        with small_schedule():
            legacy = ReverseDraw().to_dict()
            legacy["notification_batches"] = [
                {
                    "status": "sending",
                    "allocation_fingerprint": "fingerprint-one",
                    "created_at": TIMESTAMP,
                    "jobs": [],
                },
                {
                    "status": "sending",
                    "allocation_fingerprint": "fingerprint-two",
                    "created_at": TIMESTAMP,
                    "jobs": [],
                },
            ]
            with psycopg.connect(
                TEST_DATABASE_URL, prepare_threshold=None
            ) as connection:
                with self.assertRaises(errors.UniqueViolation):
                    migrate_legacy_state(
                        connection,
                        legacy,
                        {},
                        migration_key=migration_key,
                        target_draw_id=draw_id,
                        draw_name="Rollback Integration Test",
                    )

                draw_scoped_tables = {
                    "draws": "id",
                    "draw_stages": "draw_id",
                    "draw_participants": "draw_id",
                    "tickets": "draw_id",
                    "ticket_ownership_events": "draw_id",
                    "draw_rounds": "draw_id",
                    "round_results": "draw_id",
                    "import_batches": "draw_id",
                    "email_batches": "draw_id",
                    "email_jobs": "draw_id",
                    "email_job_tickets": "draw_id",
                    "listings": "draw_id",
                    "purchase_requests": "draw_id",
                    "trades": "draw_id",
                }
                for table, column in draw_scoped_tables.items():
                    with self.subTest(table=table):
                        query = (
                            f"SELECT count(*) FROM {table} "
                            f"WHERE {column} = %s"
                        )
                        count = connection.execute(
                            query, (draw_id,)
                        ).fetchone()[0]
                        self.assertEqual(count, 0)
                ledger_count = connection.execute(
                    """
                    SELECT count(*) FROM data_migrations
                    WHERE migration_key = %s
                    """,
                    (migration_key,),
                ).fetchone()[0]
                self.assertEqual(ledger_count, 0)


if __name__ == "__main__":
    unittest.main()
