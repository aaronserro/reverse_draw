import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.db import RelationalDatabase
from app.email_service import EmailSendError
from app.repositories import Repositories, ValidationError
from app.services.holder_service import CredentialProvision
from app.services.import_service import ImportService
from app.services.notification_service import NotificationService
from scripts.apply_migrations import apply_migrations, discover_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@dataclass(frozen=True)
class FakeSettings:
    configured: bool = True
    provider: str = "test"
    sender: str = "draw@example.com"
    missing: tuple[str, ...] = ()


class RecordingClient:
    def __init__(self, error=None):
        self.error = error
        self.messages = []
        self._lock = threading.Lock()

    def send_ticket_email(self, **message):
        with self._lock:
            self.messages.append(message)
        if self.error is not None:
            raise self.error
        return {"status_code": 202, "request_id": "provider-request"}


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL is required for relational integration tests.",
)
class RelationalImportNotificationTests(unittest.TestCase):
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
                ) VALUES (%s, 'Import Test', 5, 'Winner', 'Winners', 'draft')
                """,
                (self.draw_id,),
            )
            connection.execute(
                """
                INSERT INTO draw_stages (
                    draw_id, stage_number, label, kind,
                    prize, survivor_target
                ) VALUES (%s, 1, 'Final', 'elimination', '', 1)
                """,
                (self.draw_id,),
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
        self.import_service = ImportService(
            self.database, self._credential_factory
        )
        self.notifications = NotificationService(
            self.database, self._trading_code
        )
        self.settings = FakeSettings()

    def tearDown(self):
        self.database.close()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                "DELETE FROM draws WHERE id = %s", (self.draw_id,)
            )

    def _credential_factory(self, key, display_name):
        del display_name
        return CredentialProvision(
            external_id=f"{self.draw_id.hex}-{key}",
            digest=f"digest-{key}",
            readable_code="654321",
        )

    @staticmethod
    def _trading_code(key, credential):
        del key, credential
        return "654321"

    def _repositories(self, connection):
        return Repositories(connection, self.draw_id)

    def _create_import(self, rows):
        with self.database.transaction() as connection:
            repositories = self._repositories(connection)
            batch = repositories.imports.create_batch(
                filename="holders.csv",
                source_fingerprint=uuid.uuid4().hex,
            )
            repositories.imports.add_rows(batch["id"], rows)
            return batch

    @staticmethod
    def _row(number, ticket, name, email, error=None):
        return {
            "row_number": number,
            "ticket_number": ticket,
            "holder_name": name,
            "normalized_holder_name": name.casefold() if name else None,
            "email": email,
            "raw_data": {
                "ticket": ticket,
                "name": name,
                "email": email,
            },
            "validation_error": error,
        }

    def _apply_standard_import(self):
        batch = self._create_import(
            [
                self._row(1, 1, "Alice", "alice@example.com"),
                self._row(2, 2, "Alice", "alice@example.com"),
                self._row(3, 3, "Bob", "bob@example.com"),
            ]
        )
        payload = self.import_service.apply_batch(
            batch["id"],
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )
        return batch, payload

    def test_import_applies_ownership_emails_credentials_and_fingerprint(self):
        batch, payload = self._apply_standard_import()

        self.assertEqual(payload["changed_tickets"], 3)
        self.assertEqual(payload["holder_count"], 2)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            applied = repositories.imports.get_batch(batch["id"])
            alice = repositories.participants.by_name("Alice")
            self.assertEqual(
                repositories.tickets.owner_map(),
                {1: "Alice", 2: "Alice", 3: "Bob"},
            )
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(applied["allocation_fingerprint"])
            self.assertEqual(alice["source_email"], "alice@example.com")
            self.assertIsNotNone(
                repositories.participants.credential_for_participant(
                    alice["id"]
                )
            )

    def test_invalid_or_duplicate_import_rolls_back(self):
        invalid = self._create_import(
            [
                self._row(
                    1,
                    1,
                    "Alice",
                    "alice@example.com",
                    error="Invalid source value",
                )
            ]
        )
        with self.assertRaises(ValidationError):
            self.import_service.apply_batch(
                invalid["id"],
                mode="replace",
                expected_version=1,
                actor_identifier="admin",
            )

        duplicate = self._create_import(
            [
                self._row(1, 1, "Alice", "alice@example.com"),
                self._row(2, 1, "Bob", "bob@example.com"),
            ]
        )
        with self.assertRaisesRegex(ValidationError, "appears in import rows"):
            self.import_service.apply_batch(
                duplicate["id"],
                mode="replace",
                expected_version=1,
                actor_identifier="admin",
            )
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(repositories.tickets.owner_map(), {})
            self.assertEqual(repositories.participants.list(), [])
            self.assertEqual(repositories.draws.get()["version"], 1)

    def test_preview_and_batch_snapshot_prevent_duplicate_delivery(self):
        self._apply_standard_import()
        preview = self.notifications.preview(self.settings)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["pending_people"], 2)
        self.assertEqual(preview["pending_tickets"], 3)
        self.assertNotIn("participant_id", preview["recipients"][0])

        created = self.notifications.create_batch(self.settings)
        batch_id = created["batch"]["id"]
        with self.assertRaises(ValidationError):
            self.notifications.create_batch(self.settings)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            jobs = repositories.notifications.jobs(batch_id)
            self.assertEqual(len(jobs), 2)
            alice = next(
                job
                for job in jobs
                if job["recipient_email"] == "alice@example.com"
            )
            self.assertEqual(alice["all_tickets"], [1, 2])
            self.assertEqual(alice["new_tickets"], [1, 2])

        client = RecordingClient()
        for job in jobs:
            self.notifications.process_job(job["id"], client)
        final_preview = self.notifications.preview(self.settings)
        self.assertFalse(final_preview["ready"])
        self.assertEqual(final_preview["pending_tickets"], 0)
        self.assertEqual(len(client.messages), 2)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            self.assertEqual(
                repositories.notifications.batches()[0]["status"],
                "completed",
            )

    def test_unknown_provider_outcome_blocks_retry(self):
        batch = self._create_import(
            [self._row(1, 1, "Alice", "alice@example.com")]
        )
        self.import_service.apply_batch(
            batch["id"],
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )
        created = self.notifications.create_batch(self.settings)
        with self.database.connection() as connection:
            job = self._repositories(connection).notifications.jobs(
                created["batch"]["id"]
            )[0]
        client = RecordingClient(
            EmailSendError("provider timed out", outcome_unknown=True)
        )

        outcome = self.notifications.process_job(job["id"], client)
        preview = self.notifications.preview(self.settings)

        self.assertEqual(outcome["job"]["status"], "unknown")
        self.assertEqual(outcome["batch_status"], "attention")
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("uncertain" in item["reason"] for item in preview["blocked"])
        )

    def test_known_provider_failure_marks_batch_for_attention(self):
        batch = self._create_import(
            [self._row(1, 1, "Alice", "alice@example.com")]
        )
        self.import_service.apply_batch(
            batch["id"],
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )
        created = self.notifications.create_batch(self.settings)
        with self.database.connection() as connection:
            job = self._repositories(connection).notifications.jobs(
                created["batch"]["id"]
            )[0]

        outcome = self.notifications.process_job(
            job["id"], RecordingClient(EmailSendError("rejected"))
        )

        self.assertEqual(outcome["job"]["status"], "failed")
        self.assertEqual(outcome["job"]["error"], "rejected")
        self.assertEqual(outcome["batch_status"], "attention")

    def test_concurrent_batch_creation_allows_only_one_sender(self):
        self._apply_standard_import()

        def create():
            try:
                self.notifications.create_batch(self.settings)
                return "created"
            except ValidationError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: create(), range(2)))

        self.assertCountEqual(outcomes, ["created", "blocked"])
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            sending = [
                batch
                for batch in repositories.notifications.batches()
                if batch["status"] == "sending"
            ]
            self.assertEqual(len(sending), 1)

    def test_stale_batch_is_expired_before_replacement(self):
        self._apply_standard_import()
        first = self.notifications.create_batch(self.settings)
        first_id = first["batch"]["id"]
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE email_batches
                SET created_at = now() - interval '2 hours'
                WHERE id = %s
                """,
                (first_id,),
            )

        replacement = self.notifications.create_batch(self.settings)

        self.assertNotEqual(replacement["batch"]["id"], first_id)
        with self.database.connection() as connection:
            repositories = self._repositories(connection)
            batches = {
                row["id"]: row
                for row in repositories.notifications.batches()
            }
            old_jobs = repositories.notifications.jobs(first_id)
            self.assertEqual(batches[first_id]["status"], "attention")
            self.assertTrue(all(job["status"] == "failed" for job in old_jobs))

    def test_terminal_job_is_idempotent_under_concurrent_workers(self):
        batch = self._create_import(
            [self._row(1, 1, "Alice", "alice@example.com")]
        )
        self.import_service.apply_batch(
            batch["id"],
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )
        created = self.notifications.create_batch(self.settings)
        with self.database.connection() as connection:
            job = self._repositories(connection).notifications.jobs(
                created["batch"]["id"]
            )[0]
        client = RecordingClient()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda _: self.notifications.process_job(
                        job["id"], client
                    ),
                    range(2),
                )
            )

        self.assertEqual(len(client.messages), 1)
        self.assertTrue(
            all(outcome["job"]["status"] == "sent" for outcome in outcomes)
        )
        with self.database.connection() as connection:
            saved = self._repositories(connection).notifications.lock_job(
                job["id"]
            )
            self.assertEqual(saved["attempt_count"], 1)

    def test_concurrent_jobs_finish_batch_without_touching_other_draw(self):
        self._apply_standard_import()
        created = self.notifications.create_batch(self.settings)
        with self.database.connection() as connection:
            jobs = self._repositories(connection).notifications.jobs(
                created["batch"]["id"]
            )
        unrelated_draw_id = uuid.uuid4()
        with psycopg.connect(
            TEST_DATABASE_URL, prepare_threshold=None
        ) as connection:
            connection.execute(
                """
                INSERT INTO draws (
                    id, name, total_tickets, completion_label,
                    completion_label_plural, status, version
                ) VALUES (%s, 'Unrelated Draw', 1,
                          'Winner', 'Winners', 'draft', 7)
                """,
                (unrelated_draw_id,),
            )

        client = RecordingClient()
        try:
            with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
                outcomes = list(
                    executor.map(
                        lambda job: self.notifications.process_job(
                            job["id"], client
                        ),
                        jobs,
                    )
                )

            self.assertEqual(len(client.messages), len(jobs))
            self.assertTrue(
                all(outcome["job"]["status"] == "sent" for outcome in outcomes)
            )
            with self.database.connection() as connection:
                repositories = self._repositories(connection)
                saved_jobs = repositories.notifications.jobs(
                    created["batch"]["id"]
                )
                batch = next(
                    row
                    for row in repositories.notifications.batches()
                    if row["id"] == created["batch"]["id"]
                )
                unrelated = connection.execute(
                    "SELECT status, version FROM draws WHERE id = %s",
                    (unrelated_draw_id,),
                ).fetchone()
            self.assertTrue(
                all(
                    job["status"] == "sent" and job["attempt_count"] == 1
                    for job in saved_jobs
                )
            )
            self.assertEqual(batch["status"], "completed")
            self.assertEqual(
                (unrelated["status"], unrelated["version"]), ("draft", 7)
            )
        finally:
            with psycopg.connect(
                TEST_DATABASE_URL, prepare_threshold=None
            ) as connection:
                connection.execute(
                    "DELETE FROM draws WHERE id = %s", (unrelated_draw_id,)
                )

    def test_shared_email_is_blocked_to_protect_trading_credentials(self):
        batch = self._create_import(
            [
                self._row(1, 1, "Alice", "shared@example.com"),
                self._row(2, 2, "Bob", "shared@example.com"),
            ]
        )
        self.import_service.apply_batch(
            batch["id"],
            mode="replace",
            expected_version=1,
            actor_identifier="admin",
        )

        preview = self.notifications.preview(self.settings)

        self.assertFalse(preview["ready"])
        self.assertEqual(preview["recipients"], [])
        self.assertTrue(
            any(
                "multiple participants" in row["reason"]
                for row in preview["blocked"]
            )
        )


if __name__ == "__main__":
    unittest.main()
