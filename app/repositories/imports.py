"""Relational holder-import batches and parsed source rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .base import ConflictError, Repository, ValidationError, require_row


class ImportRepository(Repository):
    def create_batch(
        self,
        *,
        filename: str,
        source_fingerprint: str,
        storage_path: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not filename.strip() or not source_fingerprint.strip():
            raise ValidationError(
                "Import filename and fingerprint are required."
            )
        return self.connection.execute(
            """
            INSERT INTO import_batches (
                id, draw_id, filename, storage_path,
                source_fingerprint, status, uploaded_at
            ) VALUES (%s, %s, %s, %s, %s, 'uploaded', COALESCE(%s, now()))
            RETURNING *
            """,
            (
                uuid4(),
                self.draw_id,
                filename.strip(),
                storage_path,
                source_fingerprint,
                uploaded_at,
            ),
        ).fetchone()

    def add_rows(
        self,
        batch_id: UUID,
        rows: Iterable[dict[str, Any]],
    ) -> int:
        batch = self.lock_batch(batch_id)
        if batch["status"] not in {"uploaded", "previewed"}:
            raise ConflictError("Only an uncommitted import can receive rows.")
        values = []
        for row in rows:
            values.append(
                (
                    batch_id,
                    int(row["row_number"]),
                    row.get("ticket_number"),
                    row.get("holder_name"),
                    row.get("normalized_holder_name"),
                    row.get("email"),
                    Jsonb(row.get("raw_data") or {}),
                    row.get("validation_error"),
                )
            )
        if values:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO import_rows (
                        import_batch_id, row_number, ticket_number,
                        holder_name, normalized_holder_name, email,
                        raw_data, validation_error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
        self.connection.execute(
            """
            UPDATE import_batches SET status = 'previewed'
            WHERE id = %s AND draw_id = %s
            """,
            (batch_id, self.draw_id),
        )
        return len(values)

    def get_batch(self, batch_id: UUID) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                """
                SELECT * FROM import_batches
                WHERE id = %s AND draw_id = %s
                """,
                (batch_id, self.draw_id),
            ).fetchone(),
            "Import batch not found.",
        )

    def lock_batch(self, batch_id: UUID) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                """
                SELECT * FROM import_batches
                WHERE id = %s AND draw_id = %s
                FOR UPDATE
                """,
                (batch_id, self.draw_id),
            ).fetchone(),
            "Import batch not found.",
        )

    def rows(self, batch_id: UUID) -> list[dict[str, Any]]:
        self.get_batch(batch_id)
        return list(
            self.connection.execute(
                """
                SELECT * FROM import_rows
                WHERE import_batch_id = %s
                ORDER BY row_number
                """,
                (batch_id,),
            ).fetchall()
        )

    def latest(self, *, applied_only: bool = False) -> dict[str, Any] | None:
        status_clause = "AND status = 'applied'" if applied_only else ""
        return self.connection.execute(
            f"""
            SELECT * FROM import_batches
            WHERE draw_id = %s {status_clause}
            ORDER BY uploaded_at DESC, id DESC
            LIMIT 1
            """,
            (self.draw_id,),
        ).fetchone()

    def mark_applied(
        self,
        batch_id: UUID,
        allocation_fingerprint: str,
    ) -> dict[str, Any]:
        batch = self.lock_batch(batch_id)
        if batch["status"] != "previewed":
            raise ConflictError("Only a previewed import can be applied.")
        return self.connection.execute(
            """
            UPDATE import_batches
            SET status = 'applied', allocation_fingerprint = %s,
                applied_at = now()
            WHERE id = %s AND draw_id = %s
            RETURNING *
            """,
            (allocation_fingerprint, batch_id, self.draw_id),
        ).fetchone()

    def mark_rejected(self, batch_id: UUID) -> None:
        batch = self.lock_batch(batch_id)
        if batch["status"] == "applied":
            raise ConflictError("An applied import cannot be rejected.")
        self.connection.execute(
            """
            UPDATE import_batches SET status = 'rejected'
            WHERE id = %s AND draw_id = %s
            """,
            (batch_id, self.draw_id),
        )

    def preview_data(self, batch_id: UUID) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        rows = self.rows(batch_id)
        return {
            "batch": batch,
            "rows": rows,
            "valid_rows": [row for row in rows if not row["validation_error"]],
            "invalid_rows": [row for row in rows if row["validation_error"]],
        }
