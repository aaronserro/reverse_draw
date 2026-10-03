"""Backfill normalized tables from an exported legacy draw_state document."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from app import config
from scripts.migration_common import (
    allocation_fingerprint,
    email_key,
    json_safe,
    load_export,
    parse_datetime,
    person_key,
    source_checksum,
    validate_legacy_document,
)


def _column_key(value: object) -> str:
    return " ".join(
        str(value or "").strip().casefold().replace("_", " ").split()
    )


def _pick_column(
    frame: pd.DataFrame,
    exact: tuple[str, ...],
    contains: str,
) -> object | None:
    columns = [(_column_key(column), column) for column in frame.columns]
    for key, column in columns:
        if key in exact:
            return column
    for key, column in columns:
        if contains in key:
            return column
    return None


def _source_frame(record: dict | None) -> pd.DataFrame | None:
    if not record or not record.get("json"):
        return None
    return pd.read_json(io.StringIO(str(record["json"])), orient="split")


def _source_emails(frame: pd.DataFrame | None) -> dict[str, str]:
    if frame is None:
        return {}
    name_column = _pick_column(
        frame,
        ("full name", "name", "holder", "ticket holder", "participant"),
        "name",
    )
    email_column = _pick_column(
        frame,
        ("hoopp email address", "email address", "email", "e-mail"),
        "email",
    )
    if name_column is None or email_column is None:
        return {}
    grouped: dict[str, set[str]] = {}
    for _, row in frame.iterrows():
        key = person_key(row[name_column])
        address = email_key(row[email_column])
        if key and address:
            grouped.setdefault(key, set()).add(address)
    return {
        key: next(iter(addresses))
        for key, addresses in grouped.items()
        if len(addresses) == 1
    }


def _import_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    ticket_column = _pick_column(
        frame,
        ("ticket", "ticket number", "ticket #", "ticket no"),
        "ticket",
    )
    name_column = _pick_column(
        frame,
        ("full name", "name", "holder", "ticket holder", "participant"),
        "name",
    )
    email_column = _pick_column(
        frame,
        ("hoopp email address", "email address", "email", "e-mail"),
        "email",
    )
    rows = []
    for offset, (_, row) in enumerate(frame.iterrows(), 1):
        ticket_number = None
        if ticket_column is not None:
            value = pd.to_numeric(row[ticket_column], errors="coerce")
            if pd.notna(value) and float(value).is_integer():
                ticket_number = int(value)
        holder_name = ""
        if name_column is not None and pd.notna(row[name_column]):
            holder_name = str(row[name_column]).strip()
        address = ""
        if email_column is not None and pd.notna(row[email_column]):
            address = email_key(row[email_column])
        raw = {
            str(column): json_safe(value)
            for column, value in row.to_dict().items()
        }
        rows.append(
            {
                "row_number": offset,
                "ticket_number": ticket_number,
                "holder_name": holder_name or None,
                "normalized_holder_name": person_key(holder_name) or None,
                "email": address or None,
                "raw_data": raw,
            }
        )
    return rows


def _batch_status(value: object) -> str:
    status = str(value or "attention").casefold()
    if status == "canceled":
        status = "cancelled"
    allowed = {"sending", "completed", "attention", "cancelled"}
    return status if status in allowed else "attention"


def _job_status(value: object) -> str:
    status = str(value or "failed").casefold()
    if status == "canceled":
        status = "cancelled"
    allowed = {"pending", "sent", "failed", "unknown", "cancelled"}
    return status if status in allowed else "failed"


def _executemany(
    connection: psycopg.Connection,
    query: str,
    parameters: list[tuple],
) -> None:
    if not parameters:
        return
    with connection.cursor() as cursor:
        cursor.executemany(query, parameters)


def migrate_legacy_state(
    connection: psycopg.Connection,
    data: dict,
    metadata: dict,
    *,
    migration_key: str,
    target_draw_id: uuid.UUID,
    draw_name: str,
) -> dict:
    draw = validate_legacy_document(data)
    checksum = source_checksum(data)
    source_updated_at = (
        parse_datetime(metadata["legacy_updated_at"])
        if metadata.get("legacy_updated_at")
        else None
    )
    now = datetime.now(timezone.utc)

    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (migration_key,),
        )
        existing = connection.execute(
            """
            SELECT source_checksum, target_draw_id, details
            FROM data_migrations
            WHERE migration_key = %s
            """,
            (migration_key,),
        ).fetchone()
        if existing is not None:
            if existing[0] != checksum or existing[1] != target_draw_id:
                raise RuntimeError(
                    "The migration key already exists for a different "
                    "source or draw."
                )
            return dict(existing[2])

        status = (
            "finished"
            if draw.finished
            else "active" if draw.started else "draft"
        )
        connection.execute(
            """
            INSERT INTO draws (
                id, name, total_tickets, completion_label,
                completion_label_plural, status, version
            ) VALUES (%s, %s, %s, %s, %s, %s, 1)
            """,
            (
                target_draw_id,
                draw_name,
                draw.total,
                draw.schedule["completion_label"],
                draw.schedule["completion_label_plural"],
                status,
            ),
        )

        stage_ids: dict[int, uuid.UUID] = {}
        for index, target in enumerate(draw.survivors, 1):
            stage_id = uuid.uuid4()
            stage_ids[index] = stage_id
            connection.execute(
                """
                INSERT INTO draw_stages (
                    id, draw_id, stage_number, label, kind, prize,
                    survivor_target
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    stage_id,
                    target_draw_id,
                    index,
                    draw.labels[index - 1],
                    draw.kinds[index - 1],
                    draw.prizes[index - 1],
                    target,
                ),
            )

        ticket_ids = {ticket: uuid.uuid4() for ticket in draw.tickets()}
        _executemany(
            connection,
            """
            INSERT INTO tickets (id, draw_id, ticket_number)
            VALUES (%s, %s, %s)
            """,
            [
                (ticket_id, target_draw_id, ticket)
                for ticket, ticket_id in ticket_ids.items()
            ],
        )

        frame = _source_frame(draw.source_dataframe)
        source_emails = _source_emails(frame)
        owner_names = {
            person_key(name): name
            for name in draw.owners.values()
            if person_key(name)
        }
        credential_names = {
            person_key(key): str(value.get("name") or key).strip()
            for key, value in draw.holder_credentials.items()
            if person_key(key)
        }
        display_names = {**credential_names, **owner_names}
        participant_ids: dict[str, uuid.UUID] = {}
        for key, display_name in sorted(display_names.items()):
            participant_id = uuid.uuid4()
            participant_ids[key] = participant_id
            connection.execute(
                """
                INSERT INTO draw_participants (
                    id, draw_id, display_name, normalized_name, source_email
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    participant_id,
                    target_draw_id,
                    display_name,
                    key,
                    source_emails.get(key),
                ),
            )

        for ticket, holder in sorted(draw.owners.items()):
            participant_id = participant_ids[person_key(holder)]
            ticket_id = ticket_ids[ticket]
            connection.execute(
                "UPDATE tickets SET owner_participant_id = %s WHERE id = %s",
                (participant_id, ticket_id),
            )
            connection.execute(
                """
                INSERT INTO ticket_ownership_events (
                    draw_id, ticket_id, to_participant_id, reason,
                    actor_type, actor_identifier
                ) VALUES (%s, %s, %s, 'initial_import', 'migration', %s)
                """,
                (target_draw_id, ticket_id, participant_id, migration_key),
            )

        for raw_key, credential in draw.holder_credentials.items():
            key = person_key(raw_key)
            participant_id = participant_ids.get(key)
            if participant_id is None:
                raise RuntimeError(
                    f"Could not map credential {raw_key!r} to a participant."
                )
            connection.execute(
                """
                INSERT INTO holder_credentials (
                    participant_id, credential_external_id, code_digest,
                    code_scheme, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    participant_id,
                    str(credential["id"]),
                    str(credential["digest"]),
                    str(credential.get("code_scheme") or "derived-v1"),
                    parse_datetime(credential.get("created_at"), default=now),
                ),
            )

        completed_round_ids: dict[int, uuid.UUID] = {}
        for record in draw.rounds:
            round_number = int(record["round"])
            round_id = uuid.uuid4()
            completed_round_ids[round_number] = round_id
            kind = str(record.get("kind") or draw.kinds[round_number - 1])
            connection.execute(
                """
                INSERT INTO draw_rounds (
                    id, draw_id, stage_id, round_number, label_snapshot,
                    kind_snapshot, prize_snapshot, seed, started_with,
                    survivor_count, status, executed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'completed', %s
                )
                """,
                (
                    round_id,
                    target_draw_id,
                    stage_ids[round_number],
                    round_number,
                    str(record["label"]),
                    kind,
                    str(record.get("prize") or ""),
                    str(record["seed"]),
                    int(record["started_with"]),
                    int(record["survivors"]),
                    parse_datetime(record.get("timestamp"), default=now),
                ),
            )
            result = "prize_selected" if kind == "prize" else "eliminated"
            eliminated = [
                int(ticket) for ticket in record.get("eliminated", [])
            ]
            _executemany(
                connection,
                """
                INSERT INTO round_results (
                    draw_id, round_id, ticket_id, result
                )
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (target_draw_id, round_id, ticket_ids[ticket], result)
                    for ticket in eliminated
                ],
            )
            if eliminated:
                _executemany(
                    connection,
                    """
                    UPDATE tickets
                    SET eliminated_round_id = %s
                    WHERE id = %s
                    """,
                    [(round_id, ticket_ids[ticket]) for ticket in eliminated],
                )

        for record in draw.undone:
            round_number = int(record["round"])
            round_id = uuid.uuid4()
            kind = str(record.get("kind") or draw.kinds[round_number - 1])
            connection.execute(
                """
                INSERT INTO draw_rounds (
                    id, draw_id, stage_id, round_number, label_snapshot,
                    kind_snapshot, prize_snapshot, seed, started_with,
                    survivor_count, status, executed_at, undone_at,
                    undone_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'undone', %s, %s, 'legacy'
                )
                """,
                (
                    round_id,
                    target_draw_id,
                    stage_ids[round_number],
                    round_number,
                    str(record["label"]),
                    kind,
                    str(record.get("prize") or ""),
                    str(record["seed"]),
                    int(record["started_with"]),
                    int(record["survivors"]),
                    parse_datetime(record.get("timestamp"), default=now),
                    parse_datetime(record.get("undone_at"), default=now),
                ),
            )
            result = "prize_selected" if kind == "prize" else "eliminated"
            _executemany(
                connection,
                """
                INSERT INTO round_results (
                    draw_id, round_id, ticket_id, result
                )
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (target_draw_id, round_id, ticket_ids[int(ticket)], result)
                    for ticket in record.get("eliminated", [])
                ],
            )

        import_batch_id = None
        if draw.source_dataframe and frame is not None:
            import_batch_id = uuid.uuid4()
            source_json = str(draw.source_dataframe.get("json") or "")
            source_fingerprint = str(
                draw.source_dataframe.get("fingerprint")
                or hashlib.sha256(source_json.encode()).hexdigest()
            )
            applied = bool(draw.allocation_source_fingerprint)
            uploaded_at = parse_datetime(
                draw.source_dataframe.get("uploaded_at"), default=now
            )
            connection.execute(
                """
                INSERT INTO import_batches (
                    id, draw_id, filename, source_fingerprint,
                    allocation_fingerprint, status, uploaded_at, applied_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    import_batch_id,
                    target_draw_id,
                    str(
                        draw.source_dataframe.get("filename")
                        or "legacy-upload"
                    ),
                    source_fingerprint,
                    draw.allocation_source_fingerprint or None,
                    "applied" if applied else "previewed",
                    uploaded_at,
                    uploaded_at if applied else None,
                ),
            )
            rows = _import_rows(frame)
            _executemany(
                connection,
                """
                INSERT INTO import_rows (
                    import_batch_id, row_number, ticket_number, holder_name,
                    normalized_holder_name, email, raw_data
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        import_batch_id,
                        row["row_number"],
                        row["ticket_number"],
                        row["holder_name"],
                        row["normalized_holder_name"],
                        row["email"],
                        Jsonb(row["raw_data"]),
                    )
                    for row in rows
                ],
            )

        email_batch_count = 0
        email_job_count = 0
        default_allocation = allocation_fingerprint(draw.owners)
        for legacy_batch in draw.notification_batches:
            batch_id = uuid.uuid4()
            email_batch_count += 1
            batch_status = _batch_status(legacy_batch.get("status"))
            created_at = parse_datetime(
                legacy_batch.get("created_at"), default=now
            )
            completed_at = None
            if batch_status != "sending":
                completed_at = parse_datetime(
                    legacy_batch.get("completed_at"), default=created_at
                )
            connection.execute(
                """
                INSERT INTO email_batches (
                    id, draw_id, import_batch_id, allocation_fingerprint,
                    status, created_at, completed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch_id,
                    target_draw_id,
                    import_batch_id,
                    str(
                        legacy_batch.get("allocation_fingerprint")
                        or default_allocation
                    ),
                    batch_status,
                    created_at,
                    completed_at,
                ),
            )
            for legacy_job in legacy_batch.get("jobs", []):
                email_job_count += 1
                job_id = uuid.uuid4()
                address = email_key(legacy_job.get("email"))
                name = str(legacy_job.get("name") or address).strip()
                job_status = _job_status(legacy_job.get("status"))
                sent_at = None
                if job_status == "sent":
                    sent_at = parse_datetime(
                        legacy_job.get("sent_at"),
                        default=completed_at or created_at,
                    )
                participant_id = participant_ids.get(person_key(name))
                connection.execute(
                    """
                    INSERT INTO email_jobs (
                        id, draw_id, batch_id, participant_id, recipient_name,
                        recipient_email, status, attempt_count,
                        provider_request_id, error, sent_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        target_draw_id,
                        batch_id,
                        participant_id,
                        name,
                        address,
                        job_status,
                        int(legacy_job.get("attempt_count") or 0),
                        str(
                            legacy_job.get("provider_request_id") or ""
                        )
                        or None,
                        str(legacy_job.get("error") or "") or None,
                        sent_at,
                        created_at,
                    ),
                )
                all_tickets = {
                    int(ticket)
                    for ticket in (
                        legacy_job.get("all_tickets")
                        or legacy_job.get("new_tickets")
                        or []
                    )
                }
                new_tickets = {
                    int(ticket) for ticket in legacy_job.get("new_tickets", [])
                }
                _executemany(
                    connection,
                    """
                    INSERT INTO email_job_tickets (
                        draw_id, job_id, ticket_id, is_new
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            target_draw_id,
                            job_id,
                            ticket_ids[ticket],
                            ticket in new_tickets,
                        )
                        for ticket in sorted(all_tickets)
                    ],
                )

        details = {
            "draw_id": str(target_draw_id),
            "tickets": draw.total,
            "assigned_tickets": len(draw.owners),
            "participants": len(participant_ids),
            "completed_rounds": len(draw.rounds),
            "undone_rounds": len(draw.undone),
            "email_batches": email_batch_count,
            "email_jobs": email_job_count,
            "source_checksum": checksum,
        }
        connection.execute(
            """
            INSERT INTO data_migrations (
                migration_key, source_updated_at, source_checksum,
                target_draw_id, details
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                migration_key,
                source_updated_at,
                checksum,
                target_draw_id,
                Jsonb(details),
            ),
        )
        return details


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--migration-key", required=True)
    parser.add_argument("--target-draw-id", type=uuid.UUID)
    parser.add_argument("--draw-name", default=config.ORG_NAME)
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", "").strip()
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    data, metadata = load_export(args.export)
    target_draw_id = args.target_draw_id or uuid.uuid4()
    with psycopg.connect(
        args.database_url, prepare_threshold=None
    ) as connection:
        details = migrate_legacy_state(
            connection,
            data,
            metadata,
            migration_key=args.migration_key,
            target_draw_id=target_draw_id,
            draw_name=args.draw_name,
        )
    print(
        f"Migrated draw {details['draw_id']} with checksum "
        f"{details['source_checksum']}."
    )


if __name__ == "__main__":
    main()
