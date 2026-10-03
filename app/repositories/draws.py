"""Draw, stage, lifecycle, and audited round persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from .base import ConflictError, Repository, ValidationError, require_row


class DrawRepository(Repository):
    def get(self) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                "SELECT * FROM draws WHERE id = %s",
                (self.draw_id,),
            ).fetchone(),
            f"Draw {self.draw_id} does not exist.",
        )

    def lock(self, expected_version: int | None = None) -> dict[str, Any]:
        draw = require_row(
            self.connection.execute(
                "SELECT * FROM draws WHERE id = %s FOR UPDATE",
                (self.draw_id,),
            ).fetchone(),
            f"Draw {self.draw_id} does not exist.",
        )
        if (
            expected_version is not None
            and draw["version"] != expected_version
        ):
            raise ConflictError(
                "The draw changed since the client loaded it; "
                "refresh and retry."
            )
        return draw

    def stages(self) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM draw_stages
                WHERE draw_id = %s
                ORDER BY stage_number
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def stage(self, stage_number: int) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                """
                SELECT * FROM draw_stages
                WHERE draw_id = %s AND stage_number = %s
                """,
                (self.draw_id, stage_number),
            ).fetchone(),
            f"Stage {stage_number} does not exist.",
        )

    def rounds(self, *, include_undone: bool = True) -> list[dict[str, Any]]:
        status_clause = "" if include_undone else "AND r.status = 'completed'"
        rows = self.connection.execute(
            f"""
            SELECT r.*, COALESCE(
                array_agg(t.ticket_number ORDER BY t.ticket_number)
                    FILTER (WHERE t.id IS NOT NULL),
                ARRAY[]::integer[]
            ) AS result_tickets
            FROM draw_rounds r
            LEFT JOIN round_results rr ON rr.round_id = r.id
            LEFT JOIN tickets t ON t.id = rr.ticket_id
            WHERE r.draw_id = %s {status_clause}
            GROUP BY r.id
            ORDER BY r.executed_at, r.round_number, r.id
            """,
            (self.draw_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def completed_rounds(self) -> list[dict[str, Any]]:
        return sorted(
            self.rounds(include_undone=False),
            key=lambda row: row["round_number"],
        )

    def latest_completed_round(self, *, lock: bool = False) -> dict[str, Any]:
        suffix = "FOR UPDATE" if lock else ""
        return require_row(
            self.connection.execute(
                f"""
                SELECT * FROM draw_rounds
                WHERE draw_id = %s AND status = 'completed'
                ORDER BY round_number DESC
                LIMIT 1
                {suffix}
                """,
                (self.draw_id,),
            ).fetchone(),
            "No rounds are available to undo.",
        )

    def insert_round(
        self,
        *,
        stage: dict[str, Any],
        seed: str,
        started_with: int,
        survivor_count: int,
        selected_ticket_ids: list[UUID],
        executed_at: datetime,
    ) -> UUID:
        if survivor_count <= 0 or survivor_count >= started_with:
            raise ValidationError("The round survivor count is invalid.")
        round_id = uuid4()
        row = self.connection.execute(
            """
            INSERT INTO draw_rounds (
                id, draw_id, stage_id, round_number, label_snapshot,
                kind_snapshot, prize_snapshot, seed, started_with,
                survivor_count, status, executed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'completed', %s
            )
            RETURNING id
            """,
            (
                round_id,
                self.draw_id,
                stage["id"],
                stage["stage_number"],
                stage["label"],
                stage["kind"],
                stage["prize"],
                seed,
                started_with,
                survivor_count,
                executed_at,
            ),
        ).fetchone()
        result_kind = (
            "prize_selected" if stage["kind"] == "prize" else "eliminated"
        )
        if selected_ticket_ids:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO round_results (
                        draw_id, round_id, ticket_id, result
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (self.draw_id, round_id, ticket_id, result_kind)
                        for ticket_id in selected_ticket_ids
                    ],
                )
        return row["id"]

    def mark_undone(
        self,
        round_id: UUID,
        *,
        undone_at: datetime,
        undone_by: str,
    ) -> None:
        row = self.connection.execute(
            """
            UPDATE draw_rounds
            SET status = 'undone', undone_at = %s, undone_by = %s
            WHERE id = %s AND draw_id = %s AND status = 'completed'
            RETURNING id
            """,
            (undone_at, undone_by, round_id, self.draw_id),
        ).fetchone()
        if row is None:
            raise ConflictError("The selected round is no longer completed.")

    def increment_version(self) -> int:
        row = require_row(
            self.connection.execute(
                """
                UPDATE draws
                SET version = version + 1
                WHERE id = %s
                RETURNING version
                """,
                (self.draw_id,),
            ).fetchone(),
            f"Draw {self.draw_id} does not exist.",
        )
        return int(row["version"])

    def set_status(self, status: str) -> None:
        if status not in {"draft", "active", "finished", "archived"}:
            raise ValidationError(f"Invalid draw status: {status}")
        self.connection.execute(
            "UPDATE draws SET status = %s WHERE id = %s",
            (status, self.draw_id),
        )
