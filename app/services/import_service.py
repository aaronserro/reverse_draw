"""Atomic application of validated relational import previews."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID

from psycopg.types.json import Jsonb

from app.repositories import ConflictError, Repositories, ValidationError

from .holder_service import CredentialFactory, HolderService


class _ExistingTransaction:
    """Expose one existing connection through the database service contract."""

    def __init__(self, connection: Any, draw_id: UUID) -> None:
        self.connection = connection
        self.active_draw_id = draw_id

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        yield self.connection


class ImportService:
    def __init__(
        self,
        database: Any,
        credential_factory: CredentialFactory,
    ) -> None:
        self.database = database
        self.credential_factory = credential_factory

    def apply_batch(
        self,
        batch_id: UUID,
        *,
        mode: str,
        expected_version: int,
        actor_identifier: str,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock(expected_version)
            batch = repositories.imports.lock_batch(batch_id)
            if batch["status"] != "previewed":
                raise ConflictError("Only a previewed import can be applied.")
            rows = repositories.imports.rows(batch_id)
            if not rows:
                raise ValidationError("The import does not contain any rows.")
            invalid = [row for row in rows if row["validation_error"]]
            if invalid:
                raise ValidationError(
                    "Resolve every import-row validation error before "
                    "applying."
                )

            mapping: dict[int, str] = {}
            seen_rows: dict[int, int] = {}
            for row in rows:
                ticket = row["ticket_number"]
                name = str(row["holder_name"] or "").strip()
                if ticket is None or not name:
                    raise ValidationError(
                        f"Import row {row['row_number']} has no allocation."
                    )
                ticket = int(ticket)
                if ticket in mapping:
                    raise ValidationError(
                        f"Ticket {ticket} appears in import rows "
                        f"{seen_rows[ticket]} and {row['row_number']}."
                    )
                mapping[ticket] = name
                seen_rows[ticket] = int(row["row_number"])

            holder_service = HolderService(
                _ExistingTransaction(connection, self.database.active_draw_id),
                self.credential_factory,
            )
            payload = holder_service.apply_allocation(
                mapping,
                mode=mode,
                expected_version=expected_version,
                actor_identifier=actor_identifier,
                import_batch_id=batch_id,
            )
            self._update_source_emails(connection, rows)
            return payload

    def _update_source_emails(
        self,
        connection: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        by_person: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["normalized_holder_name"] or "").strip()
            if not key:
                continue
            person = by_person.setdefault(
                key,
                {"name": row["holder_name"], "emails": set()},
            )
            email = str(row["email"] or "").strip().casefold()
            if email:
                person["emails"].add(email)

        updates = []
        for key, source in by_person.items():
            emails = source["emails"]
            updates.append(
                {
                    "normalized_name": key,
                    "source_email": (
                        next(iter(emails)) if len(emails) == 1 else None
                    ),
                }
            )
        if not updates:
            return
        updated = connection.execute(
            """
            UPDATE draw_participants AS participant
            SET source_email = source.source_email
            FROM jsonb_to_recordset(%s::jsonb) AS source(
                normalized_name text,
                source_email text
            )
            WHERE participant.draw_id = %s
              AND participant.normalized_name = source.normalized_name
            RETURNING participant.id
            """,
            (Jsonb(updates), self.database.active_draw_id),
        ).fetchall()
        if len(updated) != len(updates):
            raise ValidationError(
                "Every imported holder must resolve to one participant."
            )
