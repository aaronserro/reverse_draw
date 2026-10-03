"""Shared validation and serialization helpers for migration commands."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.draw import DrawError, ReverseDraw


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def source_checksum(data: dict) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def person_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def email_key(value: object) -> str:
    return str(value or "").strip().casefold()


def allocation_fingerprint(owners: dict[int, str]) -> str:
    canonical = json.dumps(
        [[ticket, owners[ticket]] for ticket in sorted(owners)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_datetime(
    value: object, *, default: datetime | None = None
) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    elif default is not None:
        parsed = default
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_export(path: str | Path) -> tuple[dict, dict]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("The legacy export must contain a JSON object.")
    if "data" in document:
        data = document.get("data")
        metadata = {
            key: value for key, value in document.items() if key != "data"
        }
    else:
        data = document
        metadata = {}
    if not isinstance(data, dict):
        raise ValueError(
            "The legacy export's data field must be a JSON object."
        )
    return data, metadata


def validate_legacy_document(data: dict) -> ReverseDraw:
    """Validate invariants that must survive normalized backfill."""
    draw = ReverseDraw(data)
    if set(draw.owners) - set(draw.tickets()):
        raise DrawError("Owner mapping contains an out-of-range ticket.")

    active = set(draw.tickets())
    for expected_round, record in enumerate(draw.rounds, 1):
        round_number = int(record.get("round", 0))
        if round_number != expected_round:
            raise DrawError(
                f"Completed round {round_number} is out of sequence; "
                f"expected {expected_round}."
            )
        started_with = int(record.get("started_with", -1))
        survivors = int(record.get("survivors", -1))
        eliminated = [int(ticket) for ticket in record.get("eliminated", [])]
        if started_with != len(active):
            raise DrawError(
                f"Round {round_number} started_with is {started_with}; "
                f"expected {len(active)}."
            )
        if len(eliminated) != len(set(eliminated)):
            raise DrawError(
                f"Round {round_number} contains duplicate results."
            )
        if not set(eliminated).issubset(active):
            raise DrawError(
                f"Round {round_number} contains a ticket that was not active."
            )
        if len(active) - len(eliminated) != survivors:
            raise DrawError(
                f"Round {round_number} has an impossible survivor count."
            )
        if draw.survivors[round_number - 1] != survivors:
            raise DrawError(
                f"Round {round_number} does not match its schedule target."
            )
        active.difference_update(eliminated)

    for holder_key, credential in draw.holder_credentials.items():
        if not person_key(holder_key):
            raise DrawError("A holder credential has an empty identity key.")
        if not credential.get("id") or not credential.get("digest"):
            raise DrawError(
                f"Credential for {holder_key!r} is missing its ID or digest."
            )

    for batch in draw.notification_batches:
        seen_emails: set[str] = set()
        for job in batch.get("jobs", []):
            address = email_key(job.get("email"))
            if not address:
                raise DrawError("A notification job has no recipient email.")
            if address in seen_emails:
                raise DrawError(
                    "A notification batch contains a duplicate email job."
                )
            seen_emails.add(address)
            tickets = job.get("all_tickets") or job.get("new_tickets") or []
            if any(int(ticket) not in draw.tickets() for ticket in tickets):
                raise DrawError(
                    "A notification job references an invalid ticket."
                )

    return draw


def json_safe(value: object) -> object:
    if value is None:
        return None
    try:
        if value != value:  # NaN and pandas NA-like values.
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
