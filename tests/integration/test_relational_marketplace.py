import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from app.repositories import ConflictError, Repositories, ValidationError
from app.services.draw_service import DrawService
from app.services.marketplace_service import MarketplaceService
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalMarketplaceTests(unittest.TestCase):
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
                ) VALUES (%s, 'Marketplace Test', 5,
                          'Winner', 'Winners', 'active')
                """,
                (self.draw_id,),
            )
            connection.execute(
                """
                INSERT INTO draw_stages (
                    draw_id, stage_number, label, kind,
                    prize, survivor_target
                ) VALUES
                    (%s, 1, 'Round 1', 'elimination', '', 3),
                    (%s, 2, 'Prize', 'prize', 'Gift Card', 2)
                """,
                (self.draw_id, self.draw_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tickets (draw_id, ticket_number)
                    VALUES (%s, %s)
                    """,
                    [(self.draw_id, number) for number in range(1, 6)],
                )
        self.database = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)
        self.service = MarketplaceService(
            self.database,
            minimum_price_cents=100,
            maximum_price_cents=10_000,
            request_ttl_seconds=3600,
        )
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            self.seller = repositories.participants.upsert("Seller")
            self.buyer = repositories.participants.upsert("Buyer")
            self.other_buyer = repositories.participants.upsert("Other Buyer")
            repositories.tickets.set_owner(
                1,
                self.seller["id"],
                reason="admin_assignment",
                actor_type="test",
            )

    def tearDown(self):
        self.database.close()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id = %s", (self.draw_id,)
            )

    def _repositories(self, connection):
        return Repositories(connection, self.draw_id)

    def _listing(self, price=1000):
        return self.service.upsert_listing(
            self.seller["id"],
            1,
            price,
            expected_draw_version=1,
        )

    def test_listing_create_update_cancel_and_price_validation(self):
        with self.assertRaises(ValidationError):
            self.service.upsert_listing(
                self.seller["id"],
                1,
                99,
                expected_draw_version=1,
            )
        with self.assertRaises(ConflictError):
            self.service.upsert_listing(
                self.buyer["id"],
                1,
                1000,
                expected_draw_version=1,
            )

        listing = self._listing()
        updated = self.service.upsert_listing(
            self.seller["id"],
            1,
            1500,
            expected_draw_version=2,
            expected_listing_version=1,
        )
        self.assertEqual(updated["id"], listing["id"])
        self.assertEqual(updated["price_cents"], 1500)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ConflictError):
            self.service.upsert_listing(
                self.seller["id"],
                1,
                2000,
                expected_draw_version=2,
            )

        snapshot = self.service.cancel_listing(
            self.seller["id"],
            listing["id"],
            expected_draw_version=3,
            expected_listing_version=2,
        )
        self.assertEqual(snapshot["open_listings"], [])
        self.assertEqual(snapshot["draw_version"], 4)

    def test_request_is_idempotent_and_self_purchase_is_rejected(self):
        listing = self._listing()
        first = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="request-1",
        )
        second = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="request-1",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["offered_price_cents"], 1000)
        with self.assertRaises(ValidationError):
            self.service.request_purchase(
                self.seller["id"],
                listing["id"],
                idempotency_key="seller-request",
            )

        snapshot = self.service.snapshot(self.buyer["id"])
        self.assertEqual(
            snapshot["outgoing_requests"][0]["id"], str(first["id"])
        )
        self.assertEqual(snapshot["low_ask_cents"], 1000)
        self.assertEqual(snapshot["best_bid_cents"], 1000)
        json.dumps(snapshot)

    def test_approval_transfers_ticket_and_supersedes_competitors(self):
        listing = self._listing(price=2200)
        approved = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="buyer-request",
        )
        self.service.upsert_listing(
            self.seller["id"],
            1,
            2500,
            expected_draw_version=2,
            expected_listing_version=1,
        )
        competing = self.service.request_purchase(
            self.other_buyer["id"],
            listing["id"],
            idempotency_key="other-request",
        )

        snapshot = self.service.approve_request(
            self.seller["id"],
            approved["id"],
            expected_draw_version=3,
        )

        self.assertEqual(snapshot["draw_version"], 4)
        self.assertEqual(snapshot["open_listings"], [])
        self.assertEqual(snapshot["feed"][0]["ticket"], 1)
        self.assertEqual(snapshot["feed"][0]["price_cents"], 2200)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            ticket = repositories.tickets.by_number(1)
            accepted = repositories.marketplace.request(approved["id"])
            rejected = repositories.marketplace.request(competing["id"])
            events = repositories.tickets.ownership_events(ticket["id"])
            self.assertEqual(ticket["owner_participant_id"], self.buyer["id"])
            self.assertEqual(accepted["status"], "approved")
            self.assertEqual(rejected["status"], "superseded")
            self.assertEqual(events[-1]["reason"], "trade")
            self.assertIsNotNone(events[-1]["trade_id"])

        buyer_snapshot = self.service.snapshot(self.buyer["id"])
        self.assertEqual(buyer_snapshot["tickets"][0]["ticket"], 1)
        self.assertEqual(buyer_snapshot["last_trade"]["buyer_name"], "Buyer")

    def test_simultaneous_approvals_settle_only_one_trade(self):
        listing = self._listing()
        requests = [
            self.service.request_purchase(
                buyer_id,
                listing["id"],
                idempotency_key=f"request-{index}",
            )
            for index, buyer_id in enumerate(
                (self.buyer["id"], self.other_buyer["id"]), start=1
            )
        ]

        def approve(request):
            try:
                self.service.approve_request(
                    self.seller["id"],
                    request["id"],
                    expected_draw_version=2,
                )
                return "settled"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(approve, requests))

        self.assertCountEqual(outcomes, ["settled", "conflict"])
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(len(repositories.marketplace.recent_trades()), 1)
            self.assertEqual(repositories.draws.get()["version"], 3)

    def test_settlement_failure_rolls_back_trade_and_ownership(self):
        listing = self._listing()
        request = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="rollback-request",
        )
        with patch(
            "app.repositories.tickets.TicketRepository.set_owner",
            side_effect=RuntimeError("forced settlement failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced settlement"):
                self.service.approve_request(
                    self.seller["id"],
                    request["id"],
                    expected_draw_version=2,
                )

        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(repositories.marketplace.recent_trades(), [])
            self.assertEqual(
                repositories.tickets.by_number(1)["owner_participant_id"],
                self.seller["id"],
            )
            self.assertEqual(
                repositories.marketplace.request(request["id"])["status"],
                "pending",
            )
            self.assertEqual(
                repositories.marketplace.listing(listing["id"])["status"],
                "open",
            )
            self.assertEqual(repositories.draws.get()["version"], 2)

    def test_decline_withdraw_and_expiration_enforce_authorization(self):
        listing = self._listing()
        declined = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="decline-request",
        )
        with self.assertRaises(ConflictError):
            self.service.decline_request(
                self.other_buyer["id"], declined["id"]
            )
        result = self.service.decline_request(
            self.seller["id"], declined["id"]
        )
        self.assertEqual(result["status"], "declined")

        withdrawn = self.service.request_purchase(
            self.other_buyer["id"],
            listing["id"],
            idempotency_key="withdraw-request",
        )
        result = self.service.withdraw_request(
            self.other_buyer["id"], withdrawn["id"]
        )
        self.assertEqual(result["status"], "withdrawn")

        expired = self.service.request_purchase(
            self.buyer["id"],
            listing["id"],
            idempotency_key="expired-request",
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE purchase_requests
                SET expires_at = %s
                WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc) - timedelta(seconds=1),
                    expired["id"],
                ),
            )
        with self.assertRaisesRegex(ConflictError, "expired"):
            self.service.approve_request(
                self.seller["id"],
                expired["id"],
                expected_draw_version=2,
            )
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(
                repositories.marketplace.request(expired["id"])["status"],
                "expired",
            )
        snapshot = self.service.snapshot(self.buyer["id"])
        self.assertIsNone(snapshot["best_bid_cents"])

    def test_eliminated_ticket_cannot_be_listed(self):
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            for number in range(2, 6):
                repositories.tickets.set_owner(
                    number,
                    self.seller["id"],
                    reason="admin_assignment",
                    actor_type="test",
                )
        payload = DrawService(self.database).run_next_round(1, seed=991)
        eliminated = payload["rounds"][0]["eliminated"][0]

        with self.assertRaisesRegex(ConflictError, "eliminated"):
            self.service.upsert_listing(
                self.seller["id"],
                eliminated,
                1000,
                expected_draw_version=2,
            )


if __name__ == "__main__":
    unittest.main()
