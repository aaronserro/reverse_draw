"""Canonical tickets, current ownership, status, and ownership audit."""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from psycopg.types.json import Jsonb

from .base import Repository, ValidationError, require_row


class TicketRepository(Repository):
    def all(self, *, lock: bool = False) -> list[dict[str, Any]]:
        suffix = "FOR UPDATE OF t" if lock else ""
        return list(
            self.connection.execute(
                f"""
                SELECT t.*, p.display_name AS owner_name,
                       p.normalized_name AS owner_key,
                       r.round_number AS eliminated_round_number,
                       r.label_snapshot AS eliminated_label,
                       r.kind_snapshot AS eliminated_kind,
                       r.prize_snapshot AS eliminated_prize
                FROM tickets t
                LEFT JOIN draw_participants p
                    ON p.id = t.owner_participant_id
                LEFT JOIN draw_rounds r
                    ON r.id = t.eliminated_round_id
                WHERE t.draw_id = %s
                ORDER BY t.ticket_number
                {suffix}
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def by_number(
        self, ticket_number: int, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = "FOR UPDATE OF t" if lock else ""
        return require_row(
            self.connection.execute(
                f"""
                SELECT t.*, p.display_name AS owner_name,
                       p.normalized_name AS owner_key,
                       r.round_number AS eliminated_round_number,
                       r.label_snapshot AS eliminated_label,
                       r.kind_snapshot AS eliminated_kind,
                       r.prize_snapshot AS eliminated_prize
                FROM tickets t
                LEFT JOIN draw_participants p
                    ON p.id = t.owner_participant_id
                LEFT JOIN draw_rounds r
                    ON r.id = t.eliminated_round_id
                WHERE t.draw_id = %s AND t.ticket_number = %s
                {suffix}
                """,
                (self.draw_id, ticket_number),
            ).fetchone(),
            f"Ticket {ticket_number} does not exist.",
        )

    def active(self, *, lock: bool = False) -> list[dict[str, Any]]:
        suffix = "FOR UPDATE" if lock else ""
        return list(
            self.connection.execute(
                f"""
                SELECT id, ticket_number, owner_participant_id
                FROM tickets
                WHERE draw_id = %s AND eliminated_round_id IS NULL
                ORDER BY ticket_number
                {suffix}
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def active_count(self) -> int:
        row = self.connection.execute(
            """
            SELECT count(*) AS count
            FROM tickets
            WHERE draw_id = %s AND eliminated_round_id IS NULL
            """,
            (self.draw_id,),
        ).fetchone()
        return int(row["count"])

    def owner_map(self) -> dict[int, str]:
        rows = self.connection.execute(
            """
            SELECT t.ticket_number, p.display_name
            FROM tickets t
            JOIN draw_participants p ON p.id = t.owner_participant_id
            WHERE t.draw_id = %s
            ORDER BY t.ticket_number
            """,
            (self.draw_id,),
        ).fetchall()
        return {int(row["ticket_number"]): row["display_name"] for row in rows}

    def for_participant(self, participant_id: UUID) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT t.*, r.round_number AS eliminated_round_number,
                       r.label_snapshot AS eliminated_label,
                       r.kind_snapshot AS eliminated_kind,
                       r.prize_snapshot AS eliminated_prize
                FROM tickets t
                LEFT JOIN draw_rounds r ON r.id = t.eliminated_round_id
                WHERE t.draw_id = %s AND t.owner_participant_id = %s
                ORDER BY t.ticket_number
                """,
                (self.draw_id, participant_id),
            ).fetchall()
        )

    def holder_summary(self) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT p.id AS participant_id,
                       p.display_name AS holder,
                      p.normalized_name AS holder_key,
                       array_agg(t.ticket_number ORDER BY t.ticket_number)
                           FILTER (WHERE t.id IS NOT NULL) AS tickets,
                       count(t.id) FILTER (
                           WHERE t.eliminated_round_id IS NULL
                       )::integer AS still_in
                FROM draw_participants p
                LEFT JOIN tickets t
                    ON t.owner_participant_id = p.id
                   AND t.draw_id = p.draw_id
                WHERE p.draw_id = %s AND p.active
                GROUP BY p.id
                HAVING count(t.id) > 0
                ORDER BY still_in DESC, lower(p.display_name), p.id
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def set_owner(
        self,
        ticket_number: int,
        participant_id: UUID | None,
        *,
        reason: str,
        actor_type: str,
        actor_identifier: str | None = None,
        import_batch_id: UUID | None = None,
        trade_id: UUID | None = None,
    ) -> bool:
        allowed_reasons = {
            "initial_import",
            "admin_assignment",
            "admin_unassignment",
            "admin_correction",
            "trade",
        }
        if reason not in allowed_reasons:
            raise ValidationError(f"Invalid ownership reason: {reason}")
        ticket = self.by_number(ticket_number, lock=True)
        previous = ticket["owner_participant_id"]
        if previous == participant_id:
            return False
        self.connection.execute(
            """
            UPDATE tickets
            SET owner_participant_id = %s
            WHERE id = %s
            """,
            (participant_id, ticket["id"]),
        )
        self.connection.execute(
            """
            INSERT INTO ticket_ownership_events (
                draw_id, ticket_id, from_participant_id,
                to_participant_id, reason, import_batch_id, trade_id,
                actor_type, actor_identifier
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.draw_id,
                ticket["id"],
                previous,
                participant_id,
                reason,
                import_batch_id,
                trade_id,
                actor_type,
                actor_identifier,
            ),
        )
        return True

    def transfer_for_buy_order(
        self,
        ticket_number: int,
        participant_id: UUID,
        *,
        actor_identifier: str,
    ) -> UUID:
        """Transfer a locked ticket and return its immutable audit event ID."""
        ticket = self.by_number(ticket_number, lock=True)
        previous = ticket["owner_participant_id"]
        if previous == participant_id:
            raise ValidationError("A seller cannot buy their own ticket.")
        self.connection.execute(
            """
            UPDATE tickets
            SET owner_participant_id = %s
            WHERE id = %s
            """,
            (participant_id, ticket["id"]),
        )
        event = self.connection.execute(
            """
            INSERT INTO ticket_ownership_events (
                draw_id, ticket_id, from_participant_id,
                to_participant_id, reason, actor_type, actor_identifier
            ) VALUES (%s, %s, %s, %s, 'trade', 'trader', %s)
            RETURNING id
            """,
            (
                self.draw_id,
                ticket["id"],
                previous,
                participant_id,
                actor_identifier,
            ),
        ).fetchone()
        return event["id"]

    def set_owners(
        self,
        assignments: dict[int, UUID],
        *,
        mode: str,
        actor_type: str,
        actor_identifier: str | None = None,
        import_batch_id: UUID | None = None,
    ) -> list[UUID]:
        if mode not in {"replace", "merge"}:
            raise ValidationError("Allocation mode must be replace or merge.")
        desired = [
            {
                "ticket_number": int(ticket_number),
                "participant_id": str(participant_id),
            }
            for ticket_number, participant_id in sorted(assignments.items())
        ]
        rows = self.connection.execute(
            """
            WITH desired AS MATERIALIZED (
                SELECT ticket_number, participant_id
                FROM jsonb_to_recordset(%s::jsonb) AS source(
                    ticket_number integer,
                    participant_id uuid
                )
            ),
            locked AS MATERIALIZED (
                SELECT ticket.id, ticket.owner_participant_id AS previous_id,
                       desired.participant_id AS next_id
                FROM tickets AS ticket
                LEFT JOIN desired
                  ON desired.ticket_number = ticket.ticket_number
                WHERE ticket.draw_id = %s
                  AND (%s = 'replace' OR desired.ticket_number IS NOT NULL)
                FOR UPDATE OF ticket
            ),
            changed AS MATERIALIZED (
                SELECT * FROM locked
                WHERE previous_id IS DISTINCT FROM next_id
            ),
            updated AS (
                UPDATE tickets AS ticket
                SET owner_participant_id = changed.next_id
                FROM changed
                WHERE ticket.id = changed.id
                RETURNING ticket.id
            ),
            recorded AS (
                INSERT INTO ticket_ownership_events (
                    draw_id, ticket_id, from_participant_id,
                    to_participant_id, reason, import_batch_id,
                    actor_type, actor_identifier
                )
                SELECT %s, changed.id, changed.previous_id,
                       changed.next_id,
                       CASE
                         WHEN changed.next_id IS NULL
                           THEN 'admin_unassignment'
                        WHEN changed.previous_id IS NULL
                            AND %s::uuid IS NOT NULL
                           THEN 'initial_import'
                         ELSE 'admin_correction'
                       END,
                       %s::uuid, %s, %s
                FROM changed
                JOIN updated ON updated.id = changed.id
                RETURNING ticket_id
            )
            SELECT ticket_id FROM recorded
            """,
            (
                Jsonb(desired),
                self.draw_id,
                mode,
                self.draw_id,
                import_batch_id,
                import_batch_id,
                actor_type,
                actor_identifier,
            ),
        ).fetchall()
        return [row["ticket_id"] for row in rows]

    def clear_all_owners(
        self, *, actor_type: str, actor_identifier: str | None = None
    ) -> int:
        row = self.connection.execute(
            """
            WITH owned AS (
                SELECT id, owner_participant_id
                FROM tickets
                WHERE draw_id = %s AND owner_participant_id IS NOT NULL
                FOR UPDATE
            ), recorded AS (
                INSERT INTO ticket_ownership_events (
                    draw_id, ticket_id, from_participant_id,
                    to_participant_id, reason, actor_type, actor_identifier
                )
                SELECT %s, id, owner_participant_id, NULL,
                       'admin_unassignment', %s, %s
                FROM owned
            ), cleared AS (
                UPDATE tickets AS ticket
                SET owner_participant_id = NULL
                FROM owned
                WHERE ticket.id = owned.id
                RETURNING ticket.id
            )
            SELECT count(*)::integer AS cleared_count FROM cleared
            """,
            (
                self.draw_id,
                self.draw_id,
                actor_type,
                actor_identifier,
            ),
        ).fetchone()
        return int(row["cleared_count"])

    def set_eliminated_round(
        self, ticket_ids: Iterable[UUID], round_id: UUID
    ) -> int:
        ids = list(ticket_ids)
        if not ids:
            return 0
        result = self.connection.execute(
            """
            UPDATE tickets
            SET eliminated_round_id = %s
            WHERE draw_id = %s AND id = ANY(%s)
            """,
            (round_id, self.draw_id, ids),
        )
        return result.rowcount

    def clear_eliminated_round(self, round_id: UUID) -> int:
        result = self.connection.execute(
            """
            UPDATE tickets
            SET eliminated_round_id = NULL
            WHERE draw_id = %s AND eliminated_round_id = %s
            """,
            (self.draw_id, round_id),
        )
        return result.rowcount

    def ownership_events(self, ticket_id: UUID | None = None) -> list[dict]:
        ticket_clause = "" if ticket_id is None else "AND e.ticket_id = %s"
        parameters = (
            (self.draw_id,) if ticket_id is None else (self.draw_id, ticket_id)
        )
        return list(
            self.connection.execute(
                f"""
                SELECT e.*, t.ticket_number,
                       old.display_name AS from_name,
                       new.display_name AS to_name
                FROM ticket_ownership_events e
                JOIN tickets t ON t.id = e.ticket_id
                LEFT JOIN draw_participants old
                    ON old.id = e.from_participant_id
                LEFT JOIN draw_participants new
                    ON new.id = e.to_participant_id
                WHERE e.draw_id = %s {ticket_clause}
                ORDER BY e.created_at, e.id
                """,
                parameters,
            ).fetchall()
        )

    def export_rows(self) -> list[dict[str, Any]]:
        return self.all()
