import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from app.projections import (
    build_admin_payload,
    build_public_payload,
    build_trader_payload,
)
from app.repositories import ConflictError, Repositories
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalRepositoryTests(unittest.TestCase):
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
        self.connection = psycopg.connect(
            TEST_DATABASE_URL,
            prepare_threshold=None,
            row_factory=dict_row,
        )
        self.draw_id = uuid.uuid4()
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO draws (
                    id, name, total_tickets, completion_label,
                    completion_label_plural, status
                ) VALUES (%s, 'Repository Test', 5, 'Finalist',
                          'Finalists', 'draft')
                """,
                (self.draw_id,),
            )
            self.connection.execute(
                """
                INSERT INTO draw_stages (
                    draw_id, stage_number, label, kind,
                    prize, survivor_target
                ) VALUES
                    (%s, 1, 'Round 1', 'elimination', '', 3),
                    (%s, 2, 'Prize 1', 'prize', 'Gift Card', 2)
                """,
                (self.draw_id, self.draw_id),
            )
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tickets (draw_id, ticket_number)
                    VALUES (%s, %s)
                    """,
                    [(self.draw_id, number) for number in range(1, 6)],
                )
        self.repositories = Repositories(self.connection, self.draw_id)

    def tearDown(self):
        self.connection.rollback()
        with self.connection.transaction():
            self.connection.execute(
                "DELETE FROM draws WHERE id = %s",
                (self.draw_id,),
            )
        self.connection.close()

    def test_public_admin_and_trader_projections_share_ownership(self):
        with self.connection.transaction():
            jane = self.repositories.participants.upsert("Jane Doe")
            self.repositories.tickets.set_owner(
                1,
                jane["id"],
                reason="admin_assignment",
                actor_type="test",
            )
            self.repositories.participants.create_credential(
                jane["id"],
                external_id="jane-id",
                digest="jane-digest",
            )

        public = build_public_payload(
            self.repositories,
            show_holder_names=False,
            show_winner_name=True,
        )
        admin = build_admin_payload(self.repositories)
        trader = build_trader_payload(self.repositories, jane["id"])

        self.assertIsNone(public["holders"])
        self.assertEqual(admin["holders"], {"1": "Jane Doe"})
        self.assertEqual(admin["summary"][0]["tickets"], [1])
        self.assertEqual(trader["name"], "Jane Doe")
        self.assertEqual(trader["tickets"][0]["ticket"], 1)

    def test_expected_version_conflict_is_detected_after_lock(self):
        with self.connection.transaction():
            self.repositories.draws.lock(expected_version=1)
            self.assertEqual(self.repositories.draws.increment_version(), 2)
            with self.assertRaises(ConflictError):
                self.repositories.draws.lock(expected_version=1)

    def test_relational_runtime_validates_schema_and_active_draw(self):
        database = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)
        try:
            with database.connection() as connection:
                repositories = Repositories(connection, self.draw_id)
                self.assertEqual(repositories.draws.get()["id"], self.draw_id)
        finally:
            database.close()

    def test_zero_ticket_participant_keeps_credential(self):
        with self.connection.transaction():
            participant = self.repositories.participants.upsert("Future Buyer")
            self.repositories.participants.create_credential(
                participant["id"],
                external_id="future-buyer-id",
                digest="future-buyer-digest",
            )

        credential = self.repositories.participants.credential_by_external_id(
            "future-buyer-id"
        )
        payload = build_trader_payload(
            self.repositories, participant["id"]
        )

        self.assertIsNotNone(credential)
        self.assertEqual(payload["tickets"], [])

    def test_ownership_and_audit_event_roll_back_together(self):
        participant = None
        with self.assertRaisesRegex(RuntimeError, "force rollback"):
            with self.connection.transaction():
                participant = self.repositories.participants.upsert("Jane Doe")
                self.repositories.tickets.set_owner(
                    1,
                    participant["id"],
                    reason="admin_assignment",
                    actor_type="test",
                )
                raise RuntimeError("force rollback")

        self.assertIsNone(self.repositories.tickets.by_number(1)["owner_name"])
        self.assertEqual(self.repositories.tickets.ownership_events(), [])

    def test_import_notification_and_marketplace_queries(self):
        with self.connection.transaction():
            seller = self.repositories.participants.upsert(
                "Seller", source_email="seller@example.com"
            )
            buyer = self.repositories.participants.upsert("Buyer")
            self.repositories.tickets.set_owner(
                1,
                seller["id"],
                reason="admin_assignment",
                actor_type="test",
            )
            ticket = self.repositories.tickets.by_number(1)

            batch = self.repositories.imports.create_batch(
                filename="holders.csv",
                source_fingerprint="source-hash",
            )
            self.repositories.imports.add_rows(
                batch["id"],
                [
                    {
                        "row_number": 1,
                        "ticket_number": 1,
                        "holder_name": "Seller",
                        "normalized_holder_name": "seller",
                        "email": "seller@example.com",
                        "raw_data": {"ticket": 1, "name": "Seller"},
                    }
                ],
            )
            self.repositories.imports.mark_applied(
                batch["id"], "allocation-hash"
            )

            email_batch = self.repositories.notifications.create_batch(
                allocation_fingerprint="allocation-hash",
                import_batch_id=batch["id"],
            )
            job = self.repositories.notifications.create_job(
                email_batch["id"],
                participant_id=seller["id"],
                recipient_name="Seller",
                recipient_email="seller@example.com",
                tickets=[(ticket["id"], True)],
            )
            self.repositories.notifications.complete_job(
                job["id"],
                status="sent",
                sent_at=datetime.now(timezone.utc),
            )
            self.repositories.notifications.refresh_batch_status(
                email_batch["id"]
            )

            listing = self.repositories.marketplace.create_listing(
                ticket_id=ticket["id"],
                seller_id=seller["id"],
                price_cents=5000,
            )
            request = self.repositories.marketplace.create_request(
                listing_id=listing["id"],
                buyer_id=buyer["id"],
                offered_price_cents=5000,
                idempotency_key="request-1",
                expires_at=None,
            )

        preview = self.repositories.imports.preview_data(batch["id"])
        deliveries = self.repositories.notifications.delivery_pairs("sent")
        requests = self.repositories.marketplace.participant_requests(
            buyer["id"]
        )

        self.assertEqual(len(preview["valid_rows"]), 1)
        self.assertEqual(deliveries, {("seller@example.com", 1)})
        self.assertEqual(requests["outgoing"][0]["id"], request["id"])
        self.assertEqual(
            self.repositories.marketplace.market_summary()["low_ask_cents"],
            5000,
        )
        self.assertEqual(
            self.repositories.marketplace.market_summary()["best_bid_cents"],
            5000,
        )


if __name__ == "__main__":
    unittest.main()
