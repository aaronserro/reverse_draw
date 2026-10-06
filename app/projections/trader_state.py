"""Authenticated holder projection over normalized relational repositories."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.repositories import Repositories


def ticket_status(
    ticket: dict[str, Any],
    *,
    finished: bool,
    completion_label: str,
) -> str:
    if ticket["eliminated_round_id"] is not None:
        if ticket["eliminated_kind"] == "prize":
            return (
                ticket["eliminated_prize"]
                or ticket["eliminated_label"]
                or "Prize"
            ) + " winner"
        return f"Eliminated in {ticket['eliminated_label']}"
    if finished:
        return completion_label.upper()
    return "Still in"


def build_trader_payload(
    repositories: Repositories,
    participant_id: UUID,
    *,
    participant: dict[str, Any] | None = None,
    draw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    participant = participant or repositories.participants.by_id(
        participant_id
    )
    draw = draw or repositories.draws.get()
    stages = repositories.draws.stages()
    completed = repositories.draws.completed_rounds()
    finished = draw["status"] == "finished" or len(completed) >= len(stages)
    tickets = repositories.tickets.for_participant(participant_id)
    active_total = repositories.tickets.active_count()
    return {
        "authenticated": True,
        "name": participant["display_name"],
        "tickets": [
            {
                "ticket": ticket["ticket_number"],
                "status": ticket_status(
                    ticket,
                    finished=finished,
                    completion_label=draw["completion_label"],
                ),
                "active": ticket["eliminated_round_id"] is None,
            }
            for ticket in tickets
        ],
        "draw": {
            "total": draw["total_tickets"],
            "still_in": active_total,
            "rounds_done": len(completed),
            "rounds_total": len(stages),
            "finished": finished,
        },
    }
