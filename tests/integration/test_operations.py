import io
import json
import os
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from app.operations import readiness_report, request_log_record
from scripts.apply_migrations import apply_migrations, discover_migrations
from scripts.operational_check import main as operational_main

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class OperationalReadinessTests(unittest.TestCase):
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
        self.draw_id = uuid.uuid4()
        self.stage_id = uuid.uuid4()
        with psycopg.connect(
            TEST_DATABASE_URL,
            prepare_threshold=None,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                """
                INSERT INTO draws (
                    id, name, total_tickets, completion_label,
                    completion_label_plural, status
                ) VALUES (%s, 'Operations Test', 3,
                          'Winner', 'Winners', 'active')
                """,
                (self.draw_id,),
            )
            connection.execute(
                """
                INSERT INTO draw_stages (
                    id, draw_id, stage_number, label,
                    kind, prize, survivor_target
                ) VALUES (%s, %s, 1, 'Final', 'elimination', '', 1)
                """,
                (self.stage_id, self.draw_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tickets (draw_id, ticket_number)
                    VALUES (%s, %s)
                    """,
                    [(self.draw_id, number) for number in range(1, 4)],
                )
        self.database = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)

    def tearDown(self):
        self.database.close()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id = %s", (self.draw_id,)
            )

    def test_healthy_report_is_sanitized_and_contains_pool_status(self):
        with self.database.transaction() as connection:
            participant_id = uuid.uuid4()
            connection.execute(
                """
                INSERT INTO draw_participants (
                    id, draw_id, display_name, normalized_name, source_email
                ) VALUES (%s, %s, 'Private Person', 'private person', %s)
                """,
                (participant_id, self.draw_id, "private@example.com"),
            )
            connection.execute(
                """
                INSERT INTO holder_credentials (
                    participant_id, credential_external_id,
                    code_digest, code_scheme
                ) VALUES (%s, 'private-external-id',
                          'private-secret-digest', 'derived-v1')
                """,
                (participant_id,),
            )

        report = readiness_report(self.database)
        serialized = json.dumps(report)

        self.assertTrue(report["ok"])
        self.assertTrue(report["checks"]["schema_version"]["ok"])
        self.assertTrue(report["checks"]["ticket_count"]["ok"])
        self.assertIn("pool_size", report["pool"])
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("private-secret-digest", serialized)
        self.assertNotIn(TEST_DATABASE_URL, serialized)

    def test_ticket_count_undone_round_and_stale_jobs_fail_readiness(self):
        round_id = uuid.uuid4()
        with self.database.transaction() as connection:
            ticket = connection.execute(
                """
                SELECT id FROM tickets
                WHERE draw_id = %s AND ticket_number = 1
                """,
                (self.draw_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO draw_rounds (
                    id, draw_id, stage_id, round_number, label_snapshot,
                    kind_snapshot, prize_snapshot, seed, started_with,
                    survivor_count, status, executed_at, undone_at, undone_by
                ) VALUES (
                    %s, %s, %s, 1, 'Final', 'elimination', '', '91',
                    3, 1, 'undone', now(), now(), 'test'
                )
                """,
                (round_id, self.draw_id, self.stage_id),
            )
            connection.execute(
                "UPDATE tickets SET eliminated_round_id = %s WHERE id = %s",
                (round_id, ticket["id"]),
            )
            batch_id = uuid.uuid4()
            connection.execute(
                """
                INSERT INTO email_batches (
                    id, draw_id, allocation_fingerprint, status
                ) VALUES (%s, %s, 'operations-fingerprint', 'sending')
                """,
                (batch_id, self.draw_id),
            )
            connection.execute(
                """
                INSERT INTO email_jobs (
                    draw_id, batch_id, recipient_name,
                    recipient_email, status, created_at
                ) VALUES (%s, %s, 'Private Person',
                          'private@example.com', 'pending',
                          now() - interval '2 hours')
                """,
                (self.draw_id, batch_id),
            )
            connection.execute(
                """
                DELETE FROM tickets
                WHERE draw_id = %s AND ticket_number = 3
                """,
                (self.draw_id,),
            )

        report = readiness_report(self.database, stale_job_seconds=60)

        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["ticket_count"]["ok"])
        self.assertFalse(report["checks"]["elimination_references"]["ok"])
        self.assertFalse(report["checks"]["stale_email_jobs"]["ok"])

    def test_operational_command_exit_code_matches_report(self):
        output = io.StringIO()
        with redirect_stdout(output):
            healthy_code = operational_main(
                [
                    "--database-url",
                    TEST_DATABASE_URL,
                    "--draw-id",
                    str(self.draw_id),
                ]
            )
        healthy = json.loads(output.getvalue())
        self.assertEqual(healthy_code, 0)
        self.assertTrue(healthy["ok"])
        self.assertNotIn(TEST_DATABASE_URL, output.getvalue())

        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM tickets
                WHERE draw_id = %s AND ticket_number = 3
                """,
                (self.draw_id,),
            )
        output = io.StringIO()
        with redirect_stdout(output):
            unhealthy_code = operational_main(
                [
                    "--database-url",
                    TEST_DATABASE_URL,
                    "--draw-id",
                    str(self.draw_id),
                ]
            )
        unhealthy = json.loads(output.getvalue())
        self.assertEqual(unhealthy_code, 1)
        self.assertFalse(unhealthy["ok"])

    def test_request_telemetry_uses_only_bounded_operational_fields(self):
        record = request_log_record(
            method="post",
            route="/api/trading/login",
            status_code=409,
            duration_ms=12.345,
            storage="supabase-relational",
        )

        self.assertEqual(
            set(record),
            {
                "event",
                "method",
                "route",
                "status_code",
                "duration_ms",
                "storage",
                "expected_version_conflict",
                "transaction_failure",
            },
        )
        self.assertTrue(record["expected_version_conflict"])
        self.assertFalse(record["transaction_failure"])


if __name__ == "__main__":
    unittest.main()
