import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from app.draw import select_round_eliminations
from app.repositories import ConflictError, Repositories, ValidationError
from app.services import CredentialProvision, DrawService, HolderService
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalCommandTests(unittest.TestCase):
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
                ) VALUES (%s, 'Command Test', 6, 'Winner', 'Winners', 'draft')
                """,
                (self.draw_id,),
            )
            connection.execute(
                """
                INSERT INTO draw_stages (
                    draw_id, stage_number, label, kind,
                    prize, survivor_target
                ) VALUES
                    (%s, 1, 'Round 1', 'elimination', '', 4),
                    (%s, 2, 'Prize 1', 'prize', 'Gift Card', 3),
                    (%s, 3, 'Final', 'elimination', '', 1)
                """,
                (self.draw_id, self.draw_id, self.draw_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tickets (draw_id, ticket_number)
                    VALUES (%s, %s)
                    """,
                    [(self.draw_id, number) for number in range(1, 7)],
                )
        self.database = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)
        self.draw_service = DrawService(self.database)
        self.holder_service = HolderService(
            self.database, self._credential_factory
        )

    def tearDown(self):
        self.database.close()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id = %s", (self.draw_id,)
            )

    @staticmethod
    def _credential_factory(key, display_name):
        del display_name
        return CredentialProvision(
            external_id=f"credential-{key}",
            digest=f"digest-{key}",
            readable_code="123456",
        )

    def _repositories(self, connection):
        return Repositories(connection, self.draw_id)

    def test_round_is_deterministic_and_invalidates_marketplace(self):
        eliminated = select_round_eliminations(
            list(range(1, 7)), 4, 90210
        )
        listed_number = eliminated[0]
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            seller = repositories.participants.upsert("Seller")
            buyer = repositories.participants.upsert("Buyer")
            repositories.tickets.set_owner(
                listed_number,
                seller["id"],
                reason="admin_assignment",
                actor_type="test",
            )
            ticket = repositories.tickets.by_number(listed_number)
            listing = repositories.marketplace.create_listing(
                ticket_id=ticket["id"],
                seller_id=seller["id"],
                price_cents=1000,
            )
            request = repositories.marketplace.create_request(
                listing_id=listing["id"],
                buyer_id=buyer["id"],
                offered_price_cents=1000,
                idempotency_key="round-invalidation",
                expires_at=None,
            )

        payload = self.draw_service.run_next_round(1, seed=90210)

        self.assertEqual(payload["version"], "2")
        self.assertEqual(payload["rounds"][0]["eliminated"], eliminated)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(
                repositories.marketplace.listing(listing["id"])["status"],
                "invalidated",
            )
            self.assertEqual(
                repositories.marketplace.request(request["id"])["status"],
                "superseded",
            )

    def test_undo_restores_tickets_without_reopening_listing(self):
        eliminated = select_round_eliminations(list(range(1, 7)), 4, 7)
        listed_number = eliminated[0]
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            seller = repositories.participants.upsert("Seller")
            repositories.tickets.set_owner(
                listed_number,
                seller["id"],
                reason="admin_assignment",
                actor_type="test",
            )
            ticket = repositories.tickets.by_number(listed_number)
            listing = repositories.marketplace.create_listing(
                ticket_id=ticket["id"],
                seller_id=seller["id"],
                price_cents=1000,
            )
        self.draw_service.run_next_round(1, seed=7)

        payload = self.draw_service.undo_last_round(
            2, admin_identifier="admin@example.com"
        )

        self.assertEqual(payload["version"], "3")
        self.assertEqual(payload["rounds_done"], 0)
        self.assertTrue(all(value == 0 for value in payload["status"]))
        self.assertEqual(len(payload["undone"]), 1)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(
                repositories.marketplace.listing(listing["id"])["status"],
                "invalidated",
            )

    def test_replace_merge_blocks_and_zero_ticket_credentials(self):
        first = self.holder_service.apply_allocation(
            {1: "Alice", 2: "Alice", 3: "Bob"},
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )
        self.assertEqual(first["version"], "2")
        self.assertEqual(len(first["new_trading_credentials"]), 2)

        merged = self.holder_service.apply_allocation(
            {2: "Bob", 4: "Cara"},
            mode="merge",
            expected_version=2,
            actor_identifier="admin",
        )
        self.assertEqual(merged["version"], "3")
        with self.database.connection() as connection:
            self.assertEqual(
                self._repositories(connection).tickets.owner_map(),
                {1: "Alice", 2: "Bob", 3: "Bob", 4: "Cara"},
            )
        unassigned = self.holder_service.unassign_block(
            1,
            1,
            expected_version=3,
            actor_identifier="admin",
        )
        self.assertNotIn("1", unassigned["holders"])

        assigned = self.holder_service.assign_block(
            "Cara",
            -10,
            2,
            expected_version=4,
            actor_identifier="admin",
        )
        self.assertEqual(assigned["holders"]["1"], "Cara")
        self.assertEqual(assigned["holders"]["2"], "Cara")
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            alice = repositories.participants.by_name("Alice")
            credential = repositories.participants.credential_for_participant(
                alice["id"]
            )
            self.assertIsNotNone(credential)
            self.assertTrue(credential["active"])
            self.assertGreaterEqual(
                len(repositories.tickets.ownership_events()), 7
            )

    def test_import_application_and_validation_rollback(self):
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            batch = repositories.imports.create_batch(
                filename="holders.csv",
                source_fingerprint="source",
            )
            repositories.imports.add_rows(
                batch["id"],
                [
                    {
                        "row_number": 1,
                        "ticket_number": 1,
                        "holder_name": "Alice",
                        "normalized_holder_name": "alice",
                        "raw_data": {"ticket": 1, "name": "Alice"},
                    }
                ],
            )

        payload = self.holder_service.apply_allocation(
            {1: "Alice"},
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
            import_batch_id=batch["id"],
        )
        self.assertEqual(payload["changed_tickets"], 1)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(repositories.tickets.owner_map(), {1: "Alice"})
            applied = repositories.imports.get_batch(batch["id"])
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(applied["allocation_fingerprint"])

        with self.assertRaises(ValidationError):
            self.holder_service.apply_allocation(
                {99: "Outside"},
                mode="merge",
                expected_version=2,
                actor_identifier="admin",
            )
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertIsNone(repositories.participants.by_name("Outside"))
            self.assertEqual(repositories.draws.get()["version"], 2)

    def test_credential_failure_rolls_back_entire_allocation(self):
        def failing_factory(key, display_name):
            if key == "bob":
                raise RuntimeError("credential provider failed")
            return self._credential_factory(key, display_name)

        service = HolderService(self.database, failing_factory)
        with self.assertRaisesRegex(
            RuntimeError, "credential provider failed"
        ):
            service.apply_allocation(
                {1: "Alice", 2: "Bob"},
                mode="replace",
                expected_version=1,
                actor_identifier="admin",
            )

        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(repositories.tickets.owner_map(), {})
            self.assertEqual(repositories.participants.list(), [])
            self.assertEqual(repositories.tickets.ownership_events(), [])
            self.assertEqual(repositories.draws.get()["version"], 1)

    def test_reset_clears_execution_and_optionally_ownership(self):
        self.holder_service.assign_block(
            "Alice",
            1,
            2,
            expected_version=1,
            actor_identifier="admin",
        )
        self.draw_service.run_next_round(2, seed=11)

        payload = self.draw_service.reset_draw(
            3,
            keep_holders=False,
            admin_identifier="admin",
        )

        self.assertEqual(payload["version"], "4")
        self.assertEqual(payload["holders"], {})
        self.assertEqual(payload["rounds_done"], 0)
        self.assertEqual(len(payload["undone"]), 1)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            alice = repositories.participants.by_name("Alice")
            credential = repositories.participants.credential_for_participant(
                alice["id"]
            )
            self.assertFalse(credential["active"])
            unassignments = [
                event
                for event in repositories.tickets.ownership_events()
                if event["reason"] == "admin_unassignment"
            ]
            self.assertEqual(len(unassignments), 2)

    def test_schedule_sync_preserves_undone_round_audit_history(self):
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            original_stage_id = repositories.draws.stage(1)["id"]

        self.draw_service.run_next_round(1, seed=17)
        self.draw_service.undo_last_round(
            2, admin_identifier="admin@example.com"
        )
        updated = self.database.sync_unstarted_schedule(
            {
                "total": 6,
                "survivors": [5, 4, 3, 2],
                "labels": [
                    "Round 1",
                    "Gift Card Draw 1",
                    "Round 2",
                    "Gift Card Draw 2",
                ],
                "kinds": ["elimination", "prize", "elimination", "prize"],
                "prizes": ["", "Gift Card 1", "", "Gift Card 2"],
                "completion_label": "Finalist",
                "completion_label_plural": "Finalists",
            }
        )

        self.assertTrue(updated)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            stages = repositories.draws.stages()
            history = repositories.draws.rounds(include_undone=True)
            self.assertEqual(
                [stage["survivor_target"] for stage in stages],
                [5, 4, 3, 2],
            )
            self.assertEqual(stages[0]["id"], original_stage_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "undone")
            self.assertEqual(history[0]["label_snapshot"], "Round 1")
            self.assertEqual(history[0]["survivor_count"], 4)

    def test_same_expected_version_allows_only_one_concurrent_round(self):
        def execute(seed):
            try:
                DrawService(self.database).run_next_round(1, seed=seed)
                return "completed"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(execute, (100, 200)))

        self.assertCountEqual(outcomes, ["completed", "conflict"])
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(len(repositories.draws.completed_rounds()), 1)
            self.assertEqual(repositories.draws.get()["version"], 2)


if __name__ == "__main__":
    unittest.main()
