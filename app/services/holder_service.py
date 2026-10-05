"""Atomic relational commands for holder allocation changes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from app.projections import build_admin_payload
from app.repositories import Repositories, ValidationError
from app.repositories.base import clean_display_name, normalize_person_name


@dataclass(frozen=True)
class CredentialProvision:
    """Credential material produced by application authentication policy."""

    external_id: str
    digest: str
    scheme: str = "derived-v1"
    readable_code: str | None = None


CredentialFactory = Callable[[str, str], CredentialProvision]


class HolderService:
    """Apply ownership changes and their audit events atomically."""

    def __init__(
        self,
        database: Any,
        credential_factory: CredentialFactory,
    ) -> None:
        self.database = database
        self.credential_factory = credential_factory

    def apply_allocation(
        self,
        incoming_mapping: dict[int, str],
        *,
        mode: str,
        expected_version: int,
        actor_identifier: str,
        import_batch_id: UUID | None = None,
    ) -> dict[str, Any]:
        if mode not in {"replace", "merge"}:
            raise ValidationError("Allocation mode must be replace or merge.")
        incoming = self._clean_mapping(incoming_mapping)
        actor = actor_identifier.strip()
        if not actor:
            raise ValidationError("An administrator identifier is required.")

        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock(expected_version)
            self._validate_ticket_numbers(incoming, draw["total_tickets"])
            tickets = repositories.tickets.all(lock=True)
            current = {
                int(ticket["ticket_number"]): ticket["owner_name"]
                for ticket in tickets
                if ticket["owner_name"]
            }
            desired = dict(current) if mode == "merge" else {}
            desired.update(incoming)

            participants, generated = self._participants_for(
                repositories, desired.values()
            )
            timestamp = self._now(connection)
            ownership_source = incoming if mode == "merge" else desired
            assignments = {
                number: participants[normalize_person_name(name)]["id"]
                for number, name in ownership_source.items()
            }
            changed_ticket_ids = repositories.tickets.set_owners(
                assignments,
                mode=mode,
                actor_type="admin",
                actor_identifier=actor,
                import_batch_id=import_batch_id,
            )
            repositories.marketplace.invalidate_tickets(
                changed_ticket_ids, timestamp
            )

            if import_batch_id is not None:
                repositories.imports.mark_applied(
                    import_batch_id,
                    self._allocation_fingerprint(desired),
                )
            version = repositories.draws.increment_version()
            return {
                "version": str(version),
                "changed_tickets": len(changed_ticket_ids),
                "holder_count": len(
                    {
                        normalize_person_name(name)
                        for name in incoming.values()
                    }
                ),
                "new_trading_credentials": generated,
            }

    def assign_block(
        self,
        holder_name: str,
        start: int,
        end: int,
        *,
        expected_version: int,
        actor_identifier: str,
    ) -> dict[str, Any]:
        display = clean_display_name(holder_name)
        if not display or not normalize_person_name(display):
            raise ValidationError("Participant name is required.")
        actor = actor_identifier.strip()
        if not actor:
            raise ValidationError("An administrator identifier is required.")

        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock(expected_version)
            lower, upper = self._bounds(start, end, draw["total_tickets"])
            tickets = repositories.tickets.all(lock=True)
            participant, generated = self._participant_for(
                repositories, display
            )
            timestamp = self._now(connection)
            for ticket in tickets:
                number = int(ticket["ticket_number"])
                if not lower <= number <= upper:
                    continue
                if repositories.tickets.set_owner(
                    number,
                    participant["id"],
                    reason="admin_assignment",
                    actor_type="admin",
                    actor_identifier=actor,
                ):
                    repositories.marketplace.invalidate_ticket(
                        ticket["id"], timestamp
                    )
            repositories.draws.increment_version()
            return self._result(repositories, generated)

    def unassign_block(
        self,
        start: int,
        end: int,
        *,
        expected_version: int,
        actor_identifier: str,
    ) -> dict[str, Any]:
        actor = actor_identifier.strip()
        if not actor:
            raise ValidationError("An administrator identifier is required.")
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock(expected_version)
            lower, upper = self._bounds(start, end, draw["total_tickets"])
            tickets = repositories.tickets.all(lock=True)
            timestamp = self._now(connection)
            for ticket in tickets:
                number = int(ticket["ticket_number"])
                if (
                    lower <= number <= upper
                    and ticket["owner_participant_id"] is not None
                ):
                    repositories.tickets.set_owner(
                        number,
                        None,
                        reason="admin_unassignment",
                        actor_type="admin",
                        actor_identifier=actor,
                    )
                    repositories.marketplace.invalidate_ticket(
                        ticket["id"], timestamp
                    )
            repositories.draws.increment_version()
            return self._result(repositories, [])

    def _participants_for(
        self,
        repositories: Repositories,
        names: Any,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
        unique = {
            normalize_person_name(name): clean_display_name(name)
            for name in names
            if normalize_person_name(name)
        }
        participants = repositories.participants.upsert_many(
            [unique[key] for key in sorted(unique)]
        )
        existing = repositories.participants.credentials_for_participants(
            [participant["id"] for participant in participants.values()]
        )
        generated = []
        credentials = []
        for key in sorted(participants):
            participant = participants[key]
            credential = existing.get(participant["id"])
            if credential is not None and credential["active"]:
                continue
            provision = self.credential_factory(
                key, participant["display_name"]
            )
            credentials.append(
                {
                    "participant_id": participant["id"],
                    "external_id": provision.external_id,
                    "digest": provision.digest,
                    "scheme": provision.scheme,
                }
            )
            if provision.readable_code is not None:
                generated.append(
                    {
                        "name": participant["display_name"],
                        "code": provision.readable_code,
                    }
                )
        repositories.participants.upsert_credentials(credentials)
        return participants, generated

    def _participant_for(
        self,
        repositories: Repositories,
        display_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        participant = repositories.participants.upsert(display_name)
        credential = repositories.participants.credential_for_participant(
            participant["id"]
        )
        if credential is not None and credential["active"]:
            return participant, []
        key = participant["normalized_name"]
        provision = self.credential_factory(key, participant["display_name"])
        if credential is None:
            repositories.participants.create_credential(
                participant["id"],
                external_id=provision.external_id,
                digest=provision.digest,
                scheme=provision.scheme,
            )
        else:
            repositories.participants.rotate_credential(
                participant["id"],
                external_id=provision.external_id,
                digest=provision.digest,
                scheme=provision.scheme,
            )
        generated = []
        if provision.readable_code is not None:
            generated.append(
                {
                    "name": participant["display_name"],
                    "code": provision.readable_code,
                }
            )
        return participant, generated

    @staticmethod
    def _clean_mapping(mapping: dict[int, str]) -> dict[int, str]:
        cleaned = {}
        for raw_ticket, raw_name in mapping.items():
            try:
                ticket = int(raw_ticket)
            except (TypeError, ValueError) as error:
                raise ValidationError(
                    "Ticket numbers must be integers."
                ) from error
            display = clean_display_name(raw_name)
            if not display or not normalize_person_name(display):
                raise ValidationError(
                    f"Ticket {ticket} must have a participant name."
                )
            cleaned[ticket] = display
        return cleaned

    @staticmethod
    def _validate_ticket_numbers(mapping: dict[int, str], total: int) -> None:
        invalid = sorted(
            ticket for ticket in mapping if not 1 <= ticket <= total
        )
        if invalid:
            raise ValidationError(
                f"Ticket numbers must be within 1-{total}: {invalid}"
            )

    @staticmethod
    def _bounds(start: int, end: int, total: int) -> tuple[int, int]:
        try:
            lower, upper = sorted((int(start), int(end)))
        except (TypeError, ValueError) as error:
            raise ValidationError("Ticket bounds must be integers.") from error
        lower, upper = max(lower, 1), min(upper, total)
        if lower > upper:
            raise ValidationError(f"Ticket range must intersect 1-{total}.")
        return lower, upper

    @staticmethod
    def _allocation_reason(
        previous_id: UUID | None,
        participant_id: UUID | None,
        import_batch_id: UUID | None,
    ) -> str:
        if participant_id is None:
            return "admin_unassignment"
        if previous_id is None and import_batch_id is not None:
            return "initial_import"
        return "admin_correction"

    @staticmethod
    def _allocation_fingerprint(mapping: dict[int, str]) -> str:
        payload = [
            [ticket, normalize_person_name(name)]
            for ticket, name in sorted(mapping.items())
        ]
        encoded = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _now(connection: Any):
        return connection.execute("SELECT now() AS value").fetchone()["value"]

    @staticmethod
    def _result(
        repositories: Repositories,
        generated: list[dict[str, str]],
    ) -> dict[str, Any]:
        payload = build_admin_payload(repositories)
        if generated:
            payload["new_trading_credentials"] = generated
        return payload
