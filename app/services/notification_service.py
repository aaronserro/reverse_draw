"""Durable relational notification previews, batches, and job processing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

from app.email_service import EmailClient, EmailSendError
from app.repositories import Repositories, ValidationError
from app.repositories.base import normalize_person_name

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TradingCodeFactory = Callable[[str, dict[str, Any]], str]


class NotificationService:
    def __init__(
        self,
        database: Any,
        trading_code_factory: TradingCodeFactory,
        *,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.database = database
        self.trading_code_factory = trading_code_factory
        self.stale_after = stale_after

    def preview(self, settings: Any) -> dict[str, Any]:
        with self.database.connection() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            return self._public_preview(
                self._preview(repositories, settings)
            )

    def create_batch(self, settings: Any) -> dict[str, Any]:
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock()
            self._expire_stale_batches(connection)
            preview = self._preview(repositories, settings)
            if not preview["ready"]:
                raise ValidationError(preview["reason"])
            matching_import = self._matching_import(
                repositories, preview["allocation_fingerprint"]
            )
            batch = repositories.notifications.create_batch(
                allocation_fingerprint=preview["allocation_fingerprint"],
                import_batch_id=(
                    matching_import["id"]
                    if matching_import is not None
                    else None
                ),
            )
            for recipient in preview["recipients"]:
                tickets = [
                    repositories.tickets.by_number(number)
                    for number in recipient["all_tickets"]
                ]
                new_numbers = set(recipient["new_tickets"])
                repositories.notifications.create_job(
                    batch["id"],
                    participant_id=recipient["participant_id"],
                    recipient_name=recipient["name"],
                    recipient_email=recipient["email"],
                    tickets=[
                        (ticket["id"], ticket["ticket_number"] in new_numbers)
                        for ticket in tickets
                    ],
                )
            public_preview = self._public_preview(preview)
            return {"batch": batch, "preview": public_preview}

    def process_job(
        self,
        job_id: UUID,
        client: EmailClient,
    ) -> dict[str, Any]:
        with self.database.connection() as connection:
            self._lock_worker(connection, job_id)
            try:
                inputs = self._load_job_inputs(connection, job_id)
                if inputs["status"] != "pending":
                    return {"job": inputs, "batch_status": None}
                try:
                    credential = inputs["credential"]
                    if credential is None or not credential["active"]:
                        raise RuntimeError(
                            "The participant has no active trading credential."
                        )
                    trading_code = self.trading_code_factory(
                        inputs["normalized_name"], credential
                    )
                    response = client.send_ticket_email(
                        recipient=inputs["recipient_email"],
                        name=inputs["recipient_name"],
                        new_tickets=inputs["new_tickets"],
                        all_tickets=inputs["all_tickets"],
                        trading_code=trading_code,
                    )
                    outcome = {
                        "status": "sent",
                        "sent_at": datetime.now(timezone.utc),
                        "error": None,
                        "provider_request_id": str(
                            response.get("request_id") or ""
                        ),
                    }
                except EmailSendError as error:
                    outcome = {
                        "status": (
                            "unknown" if error.outcome_unknown else "failed"
                        ),
                        "sent_at": None,
                        "error": self._error_text(error),
                        "provider_request_id": None,
                    }
                except Exception as error:
                    outcome = {
                        "status": "failed",
                        "sent_at": None,
                        "error": self._error_text(error),
                        "provider_request_id": None,
                    }

                with connection.transaction():
                    repositories = Repositories(
                        connection, self.database.active_draw_id
                    )
                    current = repositories.notifications.lock_job(job_id)
                    if current["status"] != "pending":
                        return {"job": current, "batch_status": None}
                    job = repositories.notifications.complete_job(
                        job_id, **outcome
                    )
                    batch_status = (
                        repositories.notifications.refresh_batch_status(
                            current["batch_id"]
                        )
                    )
                return {"job": job, "batch_status": batch_status}
            finally:
                self._unlock_worker(connection, job_id)

    def _preview(
        self,
        repositories: Repositories,
        settings: Any,
    ) -> dict[str, Any]:
        inputs = repositories.notifications.preview_inputs()
        allocations = inputs["allocations"]
        fingerprint = self._allocation_fingerprint(allocations)
        matching_import = self._matching_import(repositories, fingerprint)
        source_matches = matching_import is not None
        result = {
            "configured": bool(settings.configured),
            "provider": settings.provider,
            "sender": settings.sender,
            "missing_settings": list(settings.missing),
            "source_matches_allocation": source_matches,
            "allocation_fingerprint": fingerprint,
            "recipients": [],
            "blocked": [],
            "pending_people": 0,
            "pending_tickets": 0,
            "history": inputs["history"],
        }
        if not allocations:
            return self._not_ready(
                result, "Save ticket holders before sending email."
            )
        if not source_matches:
            return self._not_ready(
                result,
                "The saved allocation does not match the applied import.",
            )

        sent = inputs["successful_pairs"]
        unknown = inputs["unknown_pairs"]
        by_email: dict[str, dict[str, Any]] = {}
        shared_emails: set[str] = set()
        for allocation in allocations:
            email = str(allocation["source_email"] or "").strip().casefold()
            if not EMAIL_PATTERN.fullmatch(email):
                result["blocked"].append(
                    {
                        "name": allocation["display_name"],
                        "tickets": [allocation["ticket_number"]],
                        "reason": "No valid email found in the source sheet.",
                    }
                )
                continue
            recipient = by_email.setdefault(
                email,
                {
                    "email": email,
                    "name": allocation["display_name"],
                    "participant_id": allocation["participant_id"],
                    "normalized_name": allocation["normalized_name"],
                    "all_tickets": [],
                },
            )
            if recipient["participant_id"] != allocation["participant_id"]:
                shared_emails.add(email)
            recipient["all_tickets"].append(allocation["ticket_number"])

        for email, recipient in by_email.items():
            tickets = sorted(set(recipient["all_tickets"]))
            recipient["all_tickets"] = tickets
            if email in shared_emails:
                result["blocked"].append(
                    {
                        "name": recipient["name"],
                        "tickets": tickets,
                        "reason": (
                            "One email address belongs to multiple "
                            "participants."
                        ),
                    }
                )
                continue
            credential = repositories.participants.credential_for_participant(
                recipient["participant_id"]
            )
            if credential is None or not credential["active"]:
                result["blocked"].append(
                    {
                        "name": recipient["name"],
                        "tickets": tickets,
                        "reason": "No active trading credential exists.",
                    }
                )
                continue
            uncertain = [
                ticket for ticket in tickets if (email, ticket) in unknown
            ]
            if uncertain:
                result["blocked"].append(
                    {
                        "name": recipient["name"],
                        "tickets": uncertain,
                        "reason": (
                            "The provider returned an uncertain result for "
                            "these tickets."
                        ),
                    }
                )
                continue
            recipient["new_tickets"] = [
                ticket for ticket in tickets if (email, ticket) not in sent
            ]
            if recipient["new_tickets"]:
                result["recipients"].append(recipient)

        result["recipients"].sort(
            key=lambda recipient: recipient["name"].casefold()
        )
        result["pending_people"] = len(result["recipients"])
        result["pending_tickets"] = sum(
            len(recipient["new_tickets"])
            for recipient in result["recipients"]
        )
        active = inputs["active_batch"]
        active_is_fresh = bool(
            active
            and datetime.now(timezone.utc) - active["created_at"]
            < self.stale_after
        )
        if active_is_fresh:
            result["reason"] = "An email batch is currently sending."
        elif result["blocked"]:
            result["reason"] = "Fix the blocked recipients before sending."
        elif not settings.configured:
            result["reason"] = "Email delivery is not configured."
        elif not result["recipients"]:
            result["reason"] = (
                "Everyone's current tickets have already been emailed."
            )
        else:
            result["reason"] = ""
        result["sending"] = active_is_fresh
        result["ready"] = bool(
            settings.configured
            and result["recipients"]
            and not result["blocked"]
            and not active_is_fresh
        )
        return result

    def _expire_stale_batches(self, connection: Any) -> int:
        threshold = datetime.now(timezone.utc) - self.stale_after
        batches = connection.execute(
            """
            SELECT id FROM email_batches
            WHERE draw_id = %s AND status = 'sending' AND created_at < %s
            FOR UPDATE
            """,
            (self.database.active_draw_id, threshold),
        ).fetchall()
        for batch in batches:
            connection.execute(
                """
                UPDATE email_jobs
                SET status = 'failed',
                    error = %s,
                    attempt_count = attempt_count + 1
                WHERE draw_id = %s AND batch_id = %s AND status = 'pending'
                """,
                (
                    "The email worker stopped before this message was sent.",
                    self.database.active_draw_id,
                    batch["id"],
                ),
            )
            connection.execute(
                """
                UPDATE email_batches
                SET status = 'attention', completed_at = now()
                WHERE id = %s AND draw_id = %s
                """,
                (batch["id"], self.database.active_draw_id),
            )
        return len(batches)

    def _load_job_inputs(
        self, connection: Any, job_id: UUID
    ) -> dict[str, Any]:
        with connection.transaction():
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            job = repositories.notifications.lock_job(job_id)
            jobs = repositories.notifications.jobs(job["batch_id"])
            detailed = next(row for row in jobs if row["id"] == job_id)
            participant = (
                repositories.participants.by_id(job["participant_id"])
                if job["participant_id"] is not None
                else None
            )
            credential = (
                repositories.participants.credential_for_participant(
                    job["participant_id"]
                )
                if job["participant_id"] is not None
                else None
            )
            return {
                **detailed,
                "normalized_name": (
                    participant["normalized_name"] if participant else ""
                ),
                "credential": credential,
            }

    @staticmethod
    def _allocation_fingerprint(allocations: list[dict[str, Any]]) -> str:
        mapping = {
            int(row["ticket_number"]): normalize_person_name(
                row["display_name"]
            )
            for row in allocations
        }
        payload = [[ticket, name] for ticket, name in sorted(mapping.items())]
        encoded = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _matching_import(
        repositories: Repositories,
        allocation_fingerprint: str,
    ) -> dict[str, Any] | None:
        return repositories.imports.connection.execute(
            """
            SELECT * FROM import_batches
            WHERE draw_id = %s AND status = 'applied'
              AND allocation_fingerprint = %s
            ORDER BY applied_at DESC, id DESC
            LIMIT 1
            """,
            (
                repositories.imports.draw_id,
                allocation_fingerprint,
            ),
        ).fetchone()

    @staticmethod
    def _not_ready(result: dict[str, Any], reason: str) -> dict[str, Any]:
        result.update(ready=False, reason=reason, sending=False)
        return result

    @staticmethod
    def _public_preview(preview: dict[str, Any]) -> dict[str, Any]:
        public = dict(preview)
        public["recipients"] = [
            {
                key: value
                for key, value in recipient.items()
                if key not in {"participant_id", "normalized_name"}
            }
            for recipient in preview["recipients"]
        ]
        return public

    @staticmethod
    def _error_text(error: Exception) -> str:
        return str(error).strip()[:1000] or error.__class__.__name__

    @staticmethod
    def _lock_worker(connection: Any, job_id: UUID) -> None:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (str(job_id),),
            )

    @staticmethod
    def _unlock_worker(connection: Any, job_id: UUID) -> None:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (str(job_id),),
            )
