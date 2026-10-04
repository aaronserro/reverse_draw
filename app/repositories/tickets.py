"""Canonical tickets, current ownership, status, and ownership audit."""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

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
