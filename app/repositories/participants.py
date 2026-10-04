"""Stable draw participants and holder credential persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .base import (
    ConflictError,
    Repository,
    ValidationError,
    clean_display_name,
    normalize_person_name,
    require_row,
)


class ParticipantRepository(Repository):
    def by_id(self, participant_id: UUID) -> dict[str, Any]:
        return require_row(
            self.connection.execute(
                """
                SELECT * FROM draw_participants
                WHERE id = %s AND draw_id = %s
                """,
                (participant_id, self.draw_id),
            ).fetchone(),
            "Participant not found.",
        )

    def by_name(self, name: str) -> dict[str, Any] | None:
        key = normalize_person_name(name)
        if not key:
            return None
        return self.connection.execute(
            """
            SELECT * FROM draw_participants
            WHERE draw_id = %s AND normalized_name = %s
            """,
            (self.draw_id, key),
        ).fetchone()

    def upsert(
        self,
        display_name: str,
        *,
        source_email: str | None = None,
    ) -> dict[str, Any]:
        display = clean_display_name(display_name)
        key = normalize_person_name(display)
        if not display or not key:
            raise ValidationError("Participant name is required.")
        return self.connection.execute(
            """
            INSERT INTO draw_participants (
                draw_id, display_name, normalized_name, source_email
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (draw_id, normalized_name) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                source_email = COALESCE(
                    EXCLUDED.source_email,
                    draw_participants.source_email
                ),
                active = true
            RETURNING *
            """,
            (self.draw_id, display, key, source_email),
        ).fetchone()

    def list(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        active_clause = "" if include_inactive else "AND active"
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM draw_participants
                WHERE draw_id = %s {active_clause}
                ORDER BY display_name, id
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def credential_by_external_id(
        self,
        external_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any] | None:
        suffix = "FOR UPDATE OF c" if lock else ""
        return self.connection.execute(
            f"""
            SELECT c.*, p.draw_id, p.display_name, p.normalized_name,
                   p.active AS participant_active
            FROM holder_credentials c
            JOIN draw_participants p ON p.id = c.participant_id
            WHERE p.draw_id = %s AND c.credential_external_id = %s
            {suffix}
            """,
            (self.draw_id, external_id),
        ).fetchone()

    def credential_for_participant(
        self, participant_id: UUID
    ) -> dict[str, Any] | None:
        return self.connection.execute(
            """
            SELECT * FROM holder_credentials
            WHERE participant_id = %s
            """,
            (participant_id,),
        ).fetchone()

    def credentials_for_participants(
        self, participant_ids: list[UUID]
    ) -> dict[UUID, dict[str, Any]]:
        ids = list(participant_ids)
        if not ids:
            return {}
        rows = self.connection.execute(
            """
            SELECT * FROM holder_credentials
            WHERE participant_id = ANY(%s)
            """,
            (ids,),
        ).fetchall()
        return {row["participant_id"]: dict(row) for row in rows}

    def create_credential(
        self,
        participant_id: UUID,
        *,
        external_id: str,
        digest: str,
        scheme: str = "derived-v1",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.by_id(participant_id)
        if not external_id or not digest or not scheme:
            raise ValidationError("Credential fields cannot be empty.")
        try:
            return self.connection.execute(
                """
                INSERT INTO holder_credentials (
                    participant_id, credential_external_id, code_digest,
                    code_scheme, created_at
                ) VALUES (%s, %s, %s, %s, COALESCE(%s, now()))
                RETURNING *
                """,
                (participant_id, external_id, digest, scheme, created_at),
            ).fetchone()
        except Exception as error:
            if getattr(error, "sqlstate", "") == "23505":
                raise ConflictError(
                    "A credential already exists for this participant or ID."
                ) from error
            raise

    def rotate_credential(
        self,
        participant_id: UUID,
        *,
        external_id: str,
        digest: str,
        scheme: str = "derived-v1",
    ) -> dict[str, Any]:
        row = self.connection.execute(
            """
            UPDATE holder_credentials
            SET credential_external_id = %s,
                code_digest = %s,
                code_scheme = %s,
                active = true,
                rotated_at = now()
            WHERE participant_id = %s
            RETURNING *
            """,
            (external_id, digest, scheme, participant_id),
        ).fetchone()
        return require_row(row, "Participant credential does not exist.")

    def deactivate(self, participant_id: UUID) -> None:
        self.connection.execute(
            """
            UPDATE holder_credentials SET active = false
            WHERE participant_id = %s
            """,
            (participant_id,),
        )

    def deactivate_all_credentials(self) -> int:
        result = self.connection.execute(
            """
            UPDATE holder_credentials AS credential
            SET active = false
            FROM draw_participants AS participant
            WHERE credential.participant_id = participant.id
              AND participant.draw_id = %s
              AND credential.active
            """,
            (self.draw_id,),
        )
        return result.rowcount
