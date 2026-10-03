"""Durable notification batches, jobs, and delivery history queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid4

from .base import ConflictError, Repository, ValidationError, require_row


class NotificationRepository(Repository):
    def active_batch(self, *, lock: bool = False) -> dict[str, Any] | None:
        suffix = "FOR UPDATE" if lock else ""
        return self.connection.execute(
            f"""
            SELECT * FROM email_batches
            WHERE draw_id = %s AND status = 'sending'
            ORDER BY created_at DESC
            LIMIT 1
            {suffix}
            """,
            (self.draw_id,),
        ).fetchone()

    def delivery_pairs(self, status: str) -> set[tuple[str, int]]:
        if status not in {"sent", "unknown"}:
            raise ValidationError(
                "Delivery-pair status must be sent or unknown."
            )
        rows = self.connection.execute(
            """
            SELECT lower(j.recipient_email::text) AS email,
                   t.ticket_number
            FROM email_jobs j
            JOIN email_job_tickets jt ON jt.job_id = j.id
            JOIN tickets t ON t.id = jt.ticket_id
            WHERE j.draw_id = %s AND j.status = %s AND jt.is_new
            """,
            (self.draw_id, status),
        ).fetchall()
        return {(row["email"], row["ticket_number"]) for row in rows}

    def create_batch(
        self,
        *,
        allocation_fingerprint: str,
        import_batch_id: UUID | None = None,
    ) -> dict[str, Any]:
        if self.active_batch(lock=True) is not None:
            raise ConflictError("Another email batch is already sending.")
        return self.connection.execute(
            """
            INSERT INTO email_batches (
                id, draw_id, import_batch_id,
                allocation_fingerprint, status
            ) VALUES (%s, %s, %s, %s, 'sending')
            RETURNING *
            """,
            (
                uuid4(),
                self.draw_id,
                import_batch_id,
                allocation_fingerprint,
            ),
        ).fetchone()

    def create_job(
        self,
        batch_id: UUID,
        *,
        participant_id: UUID | None,
        recipient_name: str,
        recipient_email: str,
        tickets: Iterable[tuple[UUID, bool]],
    ) -> dict[str, Any]:
        job_id = uuid4()
        job = self.connection.execute(
            """
            INSERT INTO email_jobs (
                id, draw_id, batch_id, participant_id,
                recipient_name, recipient_email, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING *
            """,
            (
                job_id,
                self.draw_id,
                batch_id,
                participant_id,
                recipient_name,
                recipient_email,
            ),
        ).fetchone()
        values = [
            (self.draw_id, job_id, ticket_id, is_new)
            for ticket_id, is_new in tickets
        ]
        if values:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO email_job_tickets (
                        draw_id, job_id, ticket_id, is_new
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    values,
                )
        return job

    def batches(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM email_batches
                WHERE draw_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (self.draw_id, limit),
            ).fetchall()
        )

    def jobs(self, batch_id: UUID) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT j.*, COALESCE(
                    array_agg(t.ticket_number ORDER BY t.ticket_number)
                        FILTER (WHERE t.id IS NOT NULL),
                    ARRAY[]::integer[]
                ) AS all_tickets,
                COALESCE(
                    array_agg(t.ticket_number ORDER BY t.ticket_number)
                        FILTER (WHERE jt.is_new),
                    ARRAY[]::integer[]
                ) AS new_tickets
                FROM email_jobs j
                LEFT JOIN email_job_tickets jt ON jt.job_id = j.id
                LEFT JOIN tickets t ON t.id = jt.ticket_id
                WHERE j.draw_id = %s AND j.batch_id = %s
                GROUP BY j.id
                ORDER BY j.recipient_email
                """,
                (self.draw_id, batch_id),
            ).fetchall()
        )

    def lock_job(self, job_id: UUID) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                """
                SELECT * FROM email_jobs
                WHERE id = %s AND draw_id = %s
                FOR UPDATE
                """,
                (job_id, self.draw_id),
            ).fetchone(),
            "Email job not found.",
        )

    def complete_job(
        self,
        job_id: UUID,
        *,
        status: str,
        sent_at: datetime | None = None,
        error: str | None = None,
        provider_request_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"sent", "failed", "unknown", "cancelled"}:
            raise ValidationError("Invalid terminal email job status.")
        job = self.lock_job(job_id)
        if job["status"] != "pending":
            raise ConflictError("The email job is already terminal.")
        return self.connection.execute(
            """
            UPDATE email_jobs
            SET status = %s, sent_at = %s, error = %s,
                provider_request_id = %s,
                attempt_count = attempt_count + 1
            WHERE id = %s AND draw_id = %s
            RETURNING *
            """,
            (
                status,
                sent_at,
                error,
                provider_request_id,
                job_id,
                self.draw_id,
            ),
        ).fetchone()

    def refresh_batch_status(self, batch_id: UUID) -> str:
        require_row(
            self.connection.execute(
                """
                SELECT id FROM email_batches
                WHERE id = %s AND draw_id = %s
                FOR UPDATE
                """,
                (batch_id, self.draw_id),
            ).fetchone(),
            "Email batch not found.",
        )
        statuses = {
            row["status"]
            for row in self.connection.execute(
                """
                SELECT status FROM email_jobs
                WHERE draw_id = %s AND batch_id = %s
                """,
                (self.draw_id, batch_id),
            ).fetchall()
        }
        if not statuses or "pending" in statuses:
            return "sending"
        status = "completed" if statuses == {"sent"} else "attention"
        self.connection.execute(
            """
            UPDATE email_batches
            SET status = %s, completed_at = now()
            WHERE id = %s AND draw_id = %s
            """,
            (status, batch_id, self.draw_id),
        )
        return status

    def preview_inputs(self) -> dict[str, Any]:
        allocations = list(
            self.connection.execute(
                """
                SELECT t.id AS ticket_id, t.ticket_number,
                       p.id AS participant_id, p.display_name,
                       p.normalized_name, p.source_email
                FROM tickets t
                JOIN draw_participants p ON p.id = t.owner_participant_id
                WHERE t.draw_id = %s
                ORDER BY p.normalized_name, t.ticket_number
                """,
                (self.draw_id,),
            ).fetchall()
        )
        return {
            "allocations": allocations,
            "successful_pairs": self.delivery_pairs("sent"),
            "unknown_pairs": self.delivery_pairs("unknown"),
            "history": self.batches(),
            "active_batch": self.active_batch(),
        }
