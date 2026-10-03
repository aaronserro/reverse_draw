"""Public API projection over normalized relational repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.repositories import Repositories


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def schedule_payload(
    draw: dict[str, Any], stages: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "total": draw["total_tickets"],
        "survivors": [stage["survivor_target"] for stage in stages],
        "labels": [stage["label"] for stage in stages],
        "kinds": [stage["kind"] for stage in stages],
        "prizes": [stage["prize"] for stage in stages],
        "completion_label": draw["completion_label"],
        "completion_label_plural": draw["completion_label_plural"],
    }


def round_summary(row: dict[str, Any]) -> dict[str, Any]:
    results = list(row.get("result_tickets") or [])
    return {
        "round": row["round_number"],
        "label": row["label_snapshot"],
        "kind": row["kind_snapshot"],
        "prize": row["prize_snapshot"],
        "selected_tickets": results if row["kind_snapshot"] == "prize" else [],
        "timestamp": _iso(row["executed_at"]),
        "started_with": row["started_with"],
        "survivors": row["survivor_count"],
        "eliminated_count": len(results),
    }


def build_public_payload(
    repositories: Repositories,
    *,
    show_holder_names: bool,
    show_winner_name: bool,
) -> dict[str, Any]:
    draw = repositories.draws.get()
    stages = repositories.draws.stages()
    rounds = repositories.draws.completed_rounds()
    tickets = repositories.tickets.all()
    finished = draw["status"] == "finished" or len(rounds) >= len(stages)
    owners = {
        str(ticket["ticket_number"]): ticket["owner_name"]
        for ticket in tickets
        if ticket["owner_name"]
    }
    winners = [
        {
            "ticket": ticket["ticket_number"],
            "holder": (
                ticket["owner_name"]
                if show_holder_names or show_winner_name
                else ""
            ),
        }
        for ticket in tickets
        if finished and ticket["eliminated_round_id"] is None
    ]
    return {
        "version": str(draw["version"]),
        "schedule": schedule_payload(draw, stages),
        "started": bool(rounds),
        "finished": finished,
        "rounds_done": len(rounds),
        "next_label": (
            None if finished else stages[len(rounds)]["label"]
        ),
        "status": [
            ticket["eliminated_round_number"] or 0 for ticket in tickets
        ],
        "rounds": [round_summary(row) for row in rounds],
        "winners": winners,
        "holders": owners if show_holder_names else None,
    }
