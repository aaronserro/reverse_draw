"""Administrator API projection over normalized relational repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.repositories import Repositories

from .public_state import build_public_payload, round_summary


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def build_admin_payload(
    repositories: Repositories,
    *,
    derive_code: Callable[[str, dict[str, Any]], str] | None = None,
    public_code_set: bool = False,
) -> dict[str, Any]:
    payload = build_public_payload(
        repositories,
        show_holder_names=True,
        show_winner_name=True,
    )
    tickets = repositories.tickets.all()
    all_rounds = repositories.draws.rounds(include_undone=True)
    completed = sorted(
        [row for row in all_rounds if row["status"] == "completed"],
        key=lambda row: row["round_number"],
    )
    undone = [row for row in all_rounds if row["status"] == "undone"]
    summary = repositories.tickets.holder_summary()
    for person in summary:
        credential = repositories.participants.credential_for_participant(
            person["participant_id"]
        )
        person["trading_ready"] = bool(credential and credential["active"])
        person["trading_code"] = (
            derive_code(person["holder_key"], credential)
            if derive_code is not None and credential is not None
            else ""
        )
        person.pop("participant_id", None)
        person.pop("holder_key", None)

    payload.update(
        holders={
            str(ticket["ticket_number"]): ticket["owner_name"]
            for ticket in tickets
            if ticket["owner_name"]
        },
        rounds=[
            {
                **round_summary(row),
                "seed": row["seed"],
                "eliminated": list(row.get("result_tickets") or []),
            }
            for row in completed
        ],
        undone=[
            {
                **round_summary(row),
                "seed": row["seed"],
                "undone_at": _iso(row["undone_at"]),
                "eliminated": list(row.get("result_tickets") or []),
            }
            for row in undone
        ],
        summary=summary,
        storage="supabase-relational",
        warn_no_db=False,
        schedule_pending=False,
        public_code_set=public_code_set,
    )
    return payload
