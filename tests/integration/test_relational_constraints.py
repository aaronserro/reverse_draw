import os
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalConstraintTests(unittest.TestCase):
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
        self.other_draw_id = uuid.uuid4()
        self.seller_id = uuid.uuid4()
        self.other_participant_id = uuid.uuid4()
        self.ticket_id = uuid.uuid4()
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
                ) VALUES
                    (%s, 'Constraint Test', 2, 'Winner', 'Winners', 'active'),
                    (%s, 'Other Draw', 2, 'Winner', 'Winners', 'active')
                """,
                (self.draw_id, self.other_draw_id),
            )
            connection.execute(
                """
                INSERT INTO draw_participants (
                    id, draw_id, display_name, normalized_name
                ) VALUES
                    (%s, %s, 'Seller', 'seller'),
                    (%s, %s, 'Other', 'other')
                """,
                (
                    self.seller_id,
                    self.draw_id,
                    self.other_participant_id,
                    self.other_draw_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO tickets (id, draw_id, ticket_number)
                VALUES (%s, %s, 1)
                """,
                (self.ticket_id, self.draw_id),
            )

    def tearDown(self):
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id IN (%s, %s)",
                (self.draw_id, self.other_draw_id),
            )

    def test_same_draw_foreign_keys_and_check_constraints_are_enforced(self):
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            with self.assertRaises(errors.ForeignKeyViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE tickets SET owner_participant_id = %s
                        WHERE id = %s
                        """,
                        (self.other_participant_id, self.ticket_id),
                    )
            with self.assertRaises(errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO listings (
                            draw_id, ticket_id, seller_participant_id,
                            price_cents, status
                        ) VALUES (%s, %s, %s, -1, 'open')
                        """,
                        (self.draw_id, self.ticket_id, self.seller_id),
                    )

    def test_partial_unique_index_allows_only_one_active_listing(self):
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                """
                INSERT INTO listings (
                    draw_id, ticket_id, seller_participant_id,
                    price_cents, status
                ) VALUES (%s, %s, %s, 1000, 'open')
                """,
                (self.draw_id, self.ticket_id, self.seller_id),
            )
            with self.assertRaises(errors.UniqueViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO listings (
                            draw_id, ticket_id, seller_participant_id,
                            price_cents, status
                        ) VALUES (%s, %s, %s, 1200, 'reserved')
                        """,
                        (self.draw_id, self.ticket_id, self.seller_id),
                    )

    def test_browser_roles_cannot_read_normalized_tables(self):
        private_tables = (
            "draw_participants",
            "holder_credentials",
            "import_rows",
            "email_jobs",
            "purchase_requests",
        )
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            for role in ("anon", "authenticated"):
                for table in private_tables:
                    with self.subTest(role=role, table=table):
                        with self.assertRaises(errors.InsufficientPrivilege):
                            with connection.transaction():
                                connection.execute(
                                    f'SET LOCAL ROLE "{role}"'
                                )
                                connection.execute(f"SELECT * FROM {table}")

    def test_pool_connections_disable_preparation_and_check_schema(self):
        database = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)
        try:
            with database.connection() as connection:
                self.assertIsNone(connection.prepare_threshold)
                row = connection.execute(
                    "SELECT id FROM draws WHERE id = %s", (self.draw_id,)
                ).fetchone()
                self.assertEqual(row["id"], self.draw_id)
            database.verify_ready()
        finally:
            database.close()

        with self.assertRaisesRegex(RuntimeError, "missing test-missing"):
            RelationalDatabase(
                TEST_DATABASE_URL,
                self.draw_id,
                required_schema_version="test-missing",
            )


if __name__ == "__main__":
    unittest.main()
