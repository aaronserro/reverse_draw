import hashlib
import hmac
import os
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import config, main
from app.db import RelationalDatabase
from app.relational_api import (
    RelationalAPIContext,
    create_relational_router,
    install_relational_router,
)
from app.repositories import Repositories
from app.services.holder_service import CredentialProvision
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
TEST_SECRET = "relational-api-test-secret"


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalAPITests(unittest.TestCase):
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
                ) VALUES (%s, 'API Test', 5, 'Winner', 'Winners', 'draft')
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
        self.failures = {}
        app = FastAPI()
        app.include_router(create_relational_router(self._context()))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.database.close()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id = %s", (self.draw_id,)
            )

    def _context(self):
        return RelationalAPIContext(
            database=lambda: self.database,
            require_admin=lambda request: None,
            require_viewer=lambda request: None,
            credential_factory=self._credential_factory,
            derive_holder_code=self._derive_code,
            holder_code_digest=self._code_digest,
            sign=self._sign,
            trader_token=self._trader_token,
            set_trader_cookie=self._set_cookie,
            clear_trader_cookie=lambda response: None,
            throttle=lambda key, limit: None,
            client_ip=lambda request: "127.0.0.1",
            record_failure=lambda key: self.failures.setdefault(key, 0),
            clear_failure=lambda key: self.failures.pop(key, None),
            parse_upload=main._read_upload_dataframe,
            holders_from_frame=main._holders_from_dataframe,
            dataframe_payload=main._dataframe_payload,
            source_people=main._source_people,
        )

    @staticmethod
    def _sign(message):
        return hmac.new(
            TEST_SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

    @classmethod
    def _derive_code(cls, key, credential):
        external_id = credential.get("credential_external_id") or str(
            credential["id"]
        )
        digest = hmac.new(
            TEST_SECRET.encode(),
            f"holder-code-v1.{key}.{external_id}".encode(),
            hashlib.sha256,
        ).digest()
        return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"

    @classmethod
    def _code_digest(cls, key, code):
        return cls._sign(f"holder-code.{key}.{code}")

    @classmethod
    def _credential_factory(cls, key, display_name):
        del display_name
        external_id = f"credential-{uuid.uuid4().hex}"
        credential = {"credential_external_id": external_id}
        code = cls._derive_code(key, credential)
        return CredentialProvision(
            external_id=external_id,
            digest=cls._code_digest(key, code),
            readable_code=code,
        )

    @classmethod
    def _trader_token(cls, credential, seconds):
        external_id = credential["credential_external_id"]
        expiration = int(time.time() + seconds)
        signature = cls._sign(
            f"trader.{external_id}.{expiration}.{credential['code_digest']}"
        )
        return f"{external_id}.{expiration}.{signature}"

    @staticmethod
    def _set_cookie(response: Response, token: str, seconds: int):
        response.set_cookie(
            "rd_trader",
            token,
            max_age=seconds,
            httponly=True,
            samesite="strict",
        )

    def _repositories(self, connection):
        return Repositories(connection, self.draw_id)

    def test_public_admin_config_and_health_use_persisted_draw(self):
        health = self.client.get("/healthz")
        public = self.client.get("/api/state")
        admin = self.client.get("/api/admin/state")
        runtime_config = self.client.get("/api/config")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["storage"], "supabase-relational")
        self.assertEqual(public.json()["version"], "1")
        self.assertEqual(admin.json()["storage"], "supabase-relational")
        self.assertEqual(runtime_config.json()["schedule"]["total"], 5)

    def test_health_returns_503_for_ticket_count_mismatch(self):
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM tickets
                WHERE draw_id = %s AND ticket_number = 5
                """,
                (self.draw_id,),
            )

        health = self.client.get("/healthz")
        payload = health.json()

        self.assertEqual(health.status_code, 503)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["checks"]["ticket_count"]["ok"])
        self.assertNotIn(TEST_DATABASE_URL, health.text)

    def test_upload_apply_and_trader_login_are_relational(self):
        source = (
            b"ticket,name,email\n"
            b"1,Alice,alice@example.com\n"
            b"2,Alice,alice@example.com\n"
        )
        upload = self.client.post(
            "/api/admin/holders/file?filename=holders.csv",
            content=source,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        applied = self.client.put(
            "/api/admin/holders",
            json={
                "csv": upload.json()["csv"],
                "mode": "replace",
                "expected_version": 1,
            },
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        code = applied.json()["new_trading_credentials"][0]["code"]

        login = self.client.post(
            "/api/trading/login",
            json={"name": " alice ", "code": code},
        )
        session = self.client.get("/api/trading/session")
        preview = self.client.get("/api/admin/notifications/preview")

        self.assertEqual(login.status_code, 200, login.text)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(
            [row["ticket"] for row in session.json()["tickets"]], [1, 2]
        )
        self.assertTrue(preview.json()["source_matches_allocation"])

        class Client:
            messages = []

            def send_ticket_email(self, **message):
                self.messages.append(message)
                return {"request_id": "api-provider-request"}

        settings = SimpleNamespace(
            configured=True,
            provider="test",
            sender="draw@example.com",
            missing=[],
        )
        email_client = Client()
        with (
            patch("app.relational_api.email_config", return_value=settings),
            patch(
                "app.relational_api.build_email_client",
                return_value=email_client,
            ),
        ):
            sent = self.client.post("/api/admin/notifications/send-all")
            after = self.client.get("/api/admin/notifications/preview")
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(len(email_client.messages), 1)
        self.assertEqual(after.json()["pending_tickets"], 0)

    def test_stale_round_version_and_maintenance_are_rejected(self):
        first = self.client.post(
            "/api/admin/rounds/next", json={"expected_version": 1}
        )
        stale = self.client.post(
            "/api/admin/rounds/next", json={"expected_version": 1}
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(stale.status_code, 409, stale.text)

        with patch.object(config, "MAINTENANCE_MODE", True):
            blocked = self.client.post(
                "/api/admin/reset",
                json={
                    "expected_version": 2,
                    "keep_holders": True,
                    "confirm": "RESET",
                },
            )
            readable = self.client.get("/api/state")
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(readable.status_code, 200)

    def test_marketplace_routes_transfer_authoritative_ownership(self):
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            seller = repositories.participants.upsert("Seller")
            buyer = repositories.participants.upsert("Buyer")
            seller_material = self._credential_factory("seller", "Seller")
            buyer_material = self._credential_factory("buyer", "Buyer")
            repositories.participants.create_credential(
                seller["id"],
                external_id=seller_material.external_id,
                digest=seller_material.digest,
            )
            repositories.participants.create_credential(
                buyer["id"],
                external_id=buyer_material.external_id,
                digest=buyer_material.digest,
            )
            repositories.tickets.set_owner(
                1,
                seller["id"],
                reason="admin_assignment",
                actor_type="test",
            )
            repositories.draws.set_status("active")

        with patch.object(config, "TRADING_ENABLED", True):
            seller_login = self.client.post(
                "/api/trading/login",
                json={"name": "Seller", "code": seller_material.readable_code},
            )
            listing = self.client.post(
                "/api/trading/listings",
                json={
                    "ticket": 1,
                    "price_cents": 1200,
                    "expected_draw_version": 1,
                },
            )
            self.assertEqual(seller_login.status_code, 200, seller_login.text)
            self.assertEqual(listing.status_code, 200, listing.text)

            self.client.cookies.clear()
            buyer_login = self.client.post(
                "/api/trading/login",
                json={"name": "Buyer", "code": buyer_material.readable_code},
            )
            listing_id = listing.json()["listing_id"]
            purchase = self.client.post(
                f"/api/trading/listings/{listing_id}/requests",
                json={"idempotency_key": "api-request"},
            )
            self.assertEqual(buyer_login.status_code, 200, buyer_login.text)
            self.assertEqual(purchase.status_code, 200, purchase.text)

            self.client.cookies.clear()
            self.client.post(
                "/api/trading/login",
                json={"name": "Seller", "code": seller_material.readable_code},
            )
            request_id = purchase.json()["request_id"]
            settled = self.client.post(
                f"/api/trading/requests/{request_id}/approve",
                json={"expected_draw_version": 2},
            )
        self.assertEqual(settled.status_code, 200, settled.text)
        self.assertEqual(settled.json()["feed"][0]["ticket"], 1)
        with self.database.connection() as connection:
            owner = self._repositories(connection).tickets.by_number(1)
            self.assertEqual(owner["owner_participant_id"], buyer["id"])

    def test_restart_persistence_and_missing_draw_startup_failure(self):
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            participant = repositories.participants.upsert("Persistent")
            repositories.tickets.set_owner(
                1,
                participant["id"],
                reason="admin_assignment",
                actor_type="test",
            )
        restarted = RelationalDatabase(TEST_DATABASE_URL, self.draw_id)
        try:
            with restarted.connection() as connection:
                owners = self._repositories(connection).tickets.owner_map()
                self.assertEqual(owners, {1: "Persistent"})
        finally:
            restarted.close()

        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            RelationalDatabase(TEST_DATABASE_URL, uuid.uuid4())

    def test_router_install_replaces_legacy_path(self):
        app = FastAPI()

        @app.get("/api/state")
        def legacy_state():
            return {"legacy": True}

        install_relational_router(
            app, create_relational_router(self._context())
        )
        client = TestClient(app)
        try:
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("legacy", response.json())
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
