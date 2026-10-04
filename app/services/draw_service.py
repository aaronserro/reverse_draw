"""Atomic relational commands for draw execution and lifecycle changes."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from app import config
from app.draw import select_round_eliminations
from app.projections import build_admin_payload
from app.repositories import Repositories, ValidationError


class DrawService:
    """Execute draw mutations through one locked database transaction."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def run_next_round(
        self,
        expected_version: int,
        *,
        seed: int | str | None = None,
        executed_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = executed_at or datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock(expected_version)
            if draw["status"] in {"finished", "archived"}:
                raise ValidationError("The draw cannot run another round.")

            completed = repositories.draws.completed_rounds()
            stages = repositories.draws.stages()
            stage_number = len(completed) + 1
            if stage_number > len(stages):
                raise ValidationError("The draw is already complete.")
            stage = stages[stage_number - 1]
            active = repositories.tickets.active(lock=True)
            target = int(stage["survivor_target"])
            if len(active) <= target:
                raise ValidationError(
                    "The stage survivor target must be below the active count."
                )

            selected_seed = self._seed(stage_number - 1, seed)
            selected_numbers = select_round_eliminations(
                [int(ticket["ticket_number"]) for ticket in active],
                target,
                selected_seed,
            )
            selected_set = set(selected_numbers)
            selected = [
                ticket
                for ticket in active
                if ticket["ticket_number"] in selected_set
            ]
            if stage["kind"] == "prize" and len(selected) != 1:
                raise ValidationError(
                    "A prize stage must select exactly one ticket."
                )

            selected_ids = [ticket["id"] for ticket in selected]
            round_id = repositories.draws.insert_round(
                stage=stage,
                seed=str(selected_seed),
                started_with=len(active),
                survivor_count=target,
                selected_ticket_ids=selected_ids,
                executed_at=timestamp,
            )
            repositories.tickets.set_eliminated_round(
                selected_ids, round_id
            )
            repositories.marketplace.invalidate_tickets(
                selected_ids, timestamp
            )

            repositories.draws.set_status(
                "finished" if stage_number == len(stages) else "active"
            )
            repositories.draws.increment_version()
            return build_admin_payload(repositories)

    def undo_last_round(
        self,
        expected_version: int,
        *,
        admin_identifier: str,
        undone_at: datetime | None = None,
    ) -> dict[str, Any]:
        actor = admin_identifier.strip()
        if not actor:
            raise ValidationError("An administrator identifier is required.")
        timestamp = undone_at or datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock(expected_version)
            round_row = repositories.draws.latest_completed_round(lock=True)
            repositories.draws.mark_undone(
                round_row["id"],
                undone_at=timestamp,
                undone_by=actor,
            )
            repositories.tickets.clear_eliminated_round(round_row["id"])
            remaining = repositories.draws.completed_rounds()
            repositories.draws.set_status("active" if remaining else "draft")
            repositories.draws.increment_version()
            return build_admin_payload(repositories)

    def reset_draw(
        self,
        expected_version: int,
        *,
        keep_holders: bool,
        admin_identifier: str,
        destructive_event_reset: bool = False,
        reset_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Reset execution state while retaining immutable trade history."""
        actor = admin_identifier.strip()
        if not actor:
            raise ValidationError("An administrator identifier is required.")
        timestamp = reset_at or datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock(expected_version)
            tickets = repositories.tickets.all(lock=True)
            rounds = repositories.draws.completed_rounds()
            for round_row in reversed(rounds):
                repositories.draws.mark_undone(
                    round_row["id"],
                    undone_at=timestamp,
                    undone_by=actor,
                )
                repositories.tickets.clear_eliminated_round(round_row["id"])

            for ticket in tickets:
                repositories.marketplace.invalidate_ticket(
                    ticket["id"], timestamp
                )

            if not keep_holders:
                for ticket in tickets:
                    if ticket["owner_participant_id"] is not None:
                        repositories.tickets.set_owner(
                            ticket["ticket_number"],
                            None,
                            reason="admin_unassignment",
                            actor_type="admin",
                            actor_identifier=actor,
                        )
                for participant in repositories.participants.list():
                    repositories.participants.deactivate(participant["id"])

            # Settled trades are deliberately retained even when elevated
            # confirmation was supplied. Reversal requires a separate policy.
            _ = destructive_event_reset
            repositories.draws.set_status("draft")
            repositories.draws.increment_version()
            return build_admin_payload(repositories)

    @staticmethod
    def _seed(round_index: int, supplied: int | str | None) -> int | str:
        if supplied is not None:
            return supplied
        if config.RANDOM_SEED is not None:
            return config.RANDOM_SEED + round_index
        return secrets.randbits(64)
