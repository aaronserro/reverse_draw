import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.draw import DrawError, ReverseDraw
from scripts.apply_migrations import (
    discover_migrations,
    validate_applied_checksum,
)
from scripts.export_legacy_state import export_legacy_state
from scripts.migrate_legacy_state import migrate_legacy_state
from scripts.migration_common import source_checksum, validate_legacy_document
from scripts.reconcile_legacy_state import reconcile


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class ExportConnection:
    def __init__(self, data):
        self.data = data

    def execute(self, _query, _parameters=None):
        return Result(
            (
                self.data,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )


class ExistingMigrationConnection:
    def __init__(self, checksum, draw_id, details):
        self.row = (checksum, draw_id, details)
        self.executions = 0

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, _parameters=None):
        self.executions += 1
        if "FROM data_migrations" in query:
            return Result(self.row)
        return Result(None)


class MigrationDiscoveryTests(unittest.TestCase):
    def test_discovers_ordered_migrations_and_detects_checksum_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "002_second.sql").write_text(
                "SELECT 2;\n", encoding="utf-8"
            )
            (root / "001_first.sql").write_text(
                "SELECT 1;\n", encoding="utf-8"
            )

            migrations = discover_migrations(root)

            self.assertEqual(
                [migration.version for migration in migrations],
                ["001_first", "002_second"],
            )
            validate_applied_checksum(migrations[0], migrations[0].checksum)
            with self.assertRaisesRegex(RuntimeError, "Checksum drift"):
                validate_applied_checksum(migrations[0], "changed")

    def test_rejects_invalid_filename_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "No SQL migrations"):
                discover_migrations(root)
            (root / "bad-name.sql").write_text(
                "SELECT 1;\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Invalid migration"):
                discover_migrations(root)


class LegacyExportTests(unittest.TestCase):
    def test_export_contains_independent_artifacts_and_checksums(self):
        draw = ReverseDraw()
        draw.set_owners({1: "Jane Doe", 2: "Bob Smith"})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            manifest = export_legacy_state(
                ExportConnection(draw.to_dict()), output
            )

            self.assertEqual(
                {
                    "draw_state.json",
                    "holders.csv",
                    "tickets.csv",
                    "round-log.csv",
                    "ticket-baseline.json",
                    "checksums.json",
                },
                {path.name for path in output.iterdir()},
            )
            exported = json.loads(
                (output / "draw_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(exported["data"]["owners"]["1"], "Jane Doe")
            self.assertIn("canonical_state_sha256", manifest)
            self.assertEqual(len(manifest["draw_state.json"]), 64)


class BackfillGuardTests(unittest.TestCase):
    def test_existing_matching_migration_is_an_idempotent_replay(self):
        data = ReverseDraw().to_dict()
        draw_id = uuid.uuid4()
        details = {"draw_id": str(draw_id), "tickets": 1000}
        connection = ExistingMigrationConnection(
            source_checksum(data), draw_id, details
        )

        result = migrate_legacy_state(
            connection,
            data,
            {},
            migration_key="test-backfill",
            target_draw_id=draw_id,
            draw_name="Test Draw",
        )

        self.assertEqual(result, details)
        self.assertEqual(connection.executions, 2)

    def test_existing_migration_rejects_a_different_source(self):
        data = ReverseDraw().to_dict()
        draw_id = uuid.uuid4()
        connection = ExistingMigrationConnection(
            "different", draw_id, {"draw_id": str(draw_id)}
        )

        with self.assertRaisesRegex(RuntimeError, "different source"):
            migrate_legacy_state(
                connection,
                data,
                {},
                migration_key="test-backfill",
                target_draw_id=draw_id,
                draw_name="Test Draw",
            )

    def test_malformed_round_is_rejected_before_database_writes(self):
        data = ReverseDraw().to_dict()
        data["rounds"] = [
            {
                "round": 1,
                "label": "Round 1",
                "kind": "elimination",
                "prize": "",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "seed": "1",
                "started_with": 1000,
                "survivors": 500,
                "eliminated": [1],
            }
        ]
        connection = ExistingMigrationConnection("", uuid.uuid4(), {})

        with self.assertRaises(DrawError):
            migrate_legacy_state(
                connection,
                data,
                {},
                migration_key="invalid",
                target_draw_id=uuid.uuid4(),
                draw_name="Invalid Draw",
            )
        self.assertEqual(connection.executions, 0)


class ReconciliationTests(unittest.TestCase):
    def make_state(self):
        draw = ReverseDraw()
        draw.set_owners({1: "Jane Doe"})
        draw.holder_credentials = {
            "jane doe": {
                "id": "credential-1",
                "name": "Jane Doe",
                "digest": "digest-1",
                "code_scheme": "derived-v1",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        }
        return draw.to_dict()

    def test_identical_states_reconcile(self):
        state = self.make_state()

        report = reconcile(state, json.loads(json.dumps(state)))

        self.assertTrue(report["match"])
        self.assertEqual(report["difference_count"], 0)

    def test_ticket_difference_is_reported_with_exact_path(self):
        expected = self.make_state()
        actual = json.loads(json.dumps(expected))
        actual["owners"]["1"] = "Bob Smith"

        report = reconcile(expected, actual)

        self.assertFalse(report["match"])
        self.assertTrue(
            any(
                difference["path"] == "$.tickets.1.owner"
                for difference in report["differences"]
            )
        )

    def test_validation_accepts_the_current_empty_draw(self):
        draw = validate_legacy_document(ReverseDraw().to_dict())
        self.assertEqual(draw.total, 1000)


if __name__ == "__main__":
    unittest.main()
