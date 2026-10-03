"""Compare an exported JSON draw with its normalized relational backfill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pandas as pd
import psycopg

from app.draw import ReverseDraw
from scripts.migration_common import (
    allocation_fingerprint,
    email_key,
    load_export,
    person_key,
)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def build_relational_state(
    connection: psycopg.Connection, draw_id: UUID
) -> dict:
    draw_row = connection.execute(
        """
        SELECT total_tickets, completion_label, completion_label_plural
        FROM draws WHERE id = %s
        """,
        (draw_id,),
    ).fetchone()
    if draw_row is None:
        raise RuntimeError(f"Relational draw {draw_id} does not exist.")

    stages = connection.execute(
        """
        SELECT stage_number, label, kind, prize, survivor_target
        FROM draw_stages
        WHERE draw_id = %s
        ORDER BY stage_number
        """,
        (draw_id,),
    ).fetchall()
    schedule = {
        "total": draw_row[0],
        "survivors": [row[4] for row in stages],
        "labels": [row[1] for row in stages],
        "kinds": [row[2] for row in stages],
        "prizes": [row[3] for row in stages],
        "completion_label": draw_row[1],
        "completion_label_plural": draw_row[2],
    }

    owner_rows = connection.execute(
        """
        SELECT t.ticket_number, p.display_name
        FROM tickets t
        JOIN draw_participants p ON p.id = t.owner_participant_id
        WHERE t.draw_id = %s
        ORDER BY t.ticket_number
        """,
        (draw_id,),
    ).fetchall()
    owners = {str(ticket): name for ticket, name in owner_rows}

    result_rows = connection.execute(
        """
        SELECT r.id, t.ticket_number
        FROM round_results rr
        JOIN draw_rounds r ON r.id = rr.round_id
        JOIN tickets t ON t.id = rr.ticket_id
        WHERE r.draw_id = %s
        ORDER BY r.executed_at, r.round_number, t.ticket_number
        """,
        (draw_id,),
    ).fetchall()
    results: dict[UUID, list[int]] = {}
    for round_id, ticket in result_rows:
        results.setdefault(round_id, []).append(ticket)

    round_rows = connection.execute(
        """
        SELECT id, round_number, label_snapshot, kind_snapshot,
               prize_snapshot, seed, started_with, survivor_count,
               status, executed_at, undone_at
        FROM draw_rounds
        WHERE draw_id = %s
        ORDER BY executed_at, round_number, id
        """,
        (draw_id,),
    ).fetchall()
    rounds: list[dict] = []
    undone: list[dict] = []
    for row in round_rows:
        record = {
            "round": row[1],
            "label": row[2],
            "kind": row[3],
            "prize": row[4],
            "timestamp": _iso(row[9]),
            "seed": row[5],
            "started_with": row[6],
            "survivors": row[7],
            "eliminated": results.get(row[0], []),
        }
        if row[8] == "completed":
            rounds.append(record)
        else:
            undone.append({**record, "undone_at": _iso(row[10])})
    rounds.sort(key=lambda item: item["round"])

    credential_rows = connection.execute(
        """
        SELECT p.normalized_name, p.display_name, c.credential_external_id,
               c.code_digest, c.code_scheme, c.created_at
        FROM holder_credentials c
        JOIN draw_participants p ON p.id = c.participant_id
        WHERE p.draw_id = %s
        ORDER BY p.normalized_name
        """,
        (draw_id,),
    ).fetchall()
    credentials = {
        key: {
            "id": external_id,
            "name": display_name,
            "digest": digest,
            "code_scheme": scheme,
            "created_at": _iso(created_at),
        }
        for (
            key,
            display_name,
            external_id,
            digest,
            scheme,
            created_at,
        ) in credential_rows
    }

    source_dataframe = None
    allocation_source_fingerprint = ""
    import_row = connection.execute(
        """
         SELECT id, filename, source_fingerprint,
             allocation_fingerprint, uploaded_at
        FROM import_batches
        WHERE draw_id = %s
        ORDER BY uploaded_at DESC, id DESC
        LIMIT 1
        """,
        (draw_id,),
    ).fetchone()
    if import_row is not None:
        raw_rows = connection.execute(
            """
            SELECT raw_data
            FROM import_rows
            WHERE import_batch_id = %s
            ORDER BY row_number
            """,
            (import_row[0],),
        ).fetchall()
        frame = pd.DataFrame([dict(row[0]) for row in raw_rows])
        source_dataframe = {
            "filename": import_row[1],
            "uploaded_at": _iso(import_row[4]),
            "json": frame.to_json(orient="split", date_format="iso"),
            "fingerprint": import_row[2],
        }
        allocation_source_fingerprint = import_row[3] or ""

    batch_rows = connection.execute(
        """
        SELECT id, status, allocation_fingerprint, created_at, completed_at
        FROM email_batches
        WHERE draw_id = %s
        ORDER BY created_at, id
        """,
        (draw_id,),
    ).fetchall()
    notification_batches = []
    for batch_id, status, fingerprint, created_at, completed_at in batch_rows:
        job_rows = connection.execute(
            """
            SELECT id, recipient_email, recipient_name, status, sent_at,
                   error, provider_request_id, attempt_count
            FROM email_jobs
            WHERE batch_id = %s
            ORDER BY recipient_email
            """,
            (batch_id,),
        ).fetchall()
        jobs = []
        for job in job_rows:
            ticket_rows = connection.execute(
                """
                SELECT t.ticket_number, ejt.is_new
                FROM email_job_tickets ejt
                JOIN tickets t ON t.id = ejt.ticket_id
                WHERE ejt.job_id = %s
                ORDER BY t.ticket_number
                """,
                (job[0],),
            ).fetchall()
            jobs.append(
                {
                    "email": str(job[1]),
                    "name": job[2],
                    "status": job[3],
                    "sent_at": _iso(job[4]),
                    "error": job[5] or "",
                    "provider_request_id": job[6] or "",
                    "attempt_count": job[7],
                    "all_tickets": [row[0] for row in ticket_rows],
                    "new_tickets": [row[0] for row in ticket_rows if row[1]],
                }
            )
        notification_batches.append(
            {
                "id": str(batch_id),
                "status": status,
                "allocation_fingerprint": fingerprint,
                "created_at": _iso(created_at),
                "completed_at": _iso(completed_at),
                "jobs": jobs,
            }
        )

    return {
        "schedule": schedule,
        "owners": owners,
        "rounds": rounds,
        "undone": undone,
        "source_dataframe": source_dataframe,
        "allocation_source_fingerprint": allocation_source_fingerprint,
        "notification_batches": notification_batches,
        "holder_credentials": credentials,
    }


def _canonical_round(record: dict) -> dict:
    return {
        "round": int(record["round"]),
        "label": str(record["label"]),
        "kind": str(record.get("kind") or "elimination"),
        "prize": str(record.get("prize") or ""),
        "timestamp": str(record.get("timestamp") or ""),
        "seed": str(record["seed"]),
        "started_with": int(record["started_with"]),
        "survivors": int(record["survivors"]),
        "eliminated": sorted(
            int(ticket) for ticket in record.get("eliminated", [])
        ),
        **(
            {"undone_at": str(record.get("undone_at") or "")}
            if "undone_at" in record
            else {}
        ),
    }


def _canonical_notifications(
    batches: list[dict], default_allocation: str
) -> dict:
    batch_summaries = []
    successful: set[tuple[str, int]] = set()
    unknown: set[tuple[str, int]] = set()
    for batch in batches:
        jobs = []
        for job in batch.get("jobs", []):
            address = email_key(job.get("email"))
            new_tickets = sorted(
                int(ticket) for ticket in job.get("new_tickets", [])
            )
            all_tickets = sorted(
                int(ticket)
                for ticket in (
                    job.get("all_tickets")
                    or job.get("new_tickets")
                    or []
                )
            )
            status = str(job.get("status") or "")
            if status == "sent":
                successful.update((address, ticket) for ticket in new_tickets)
            if status == "unknown":
                unknown.update((address, ticket) for ticket in new_tickets)
            jobs.append(
                {
                    "email": address,
                    "name": str(job.get("name") or address),
                    "status": status,
                    "sent_at": str(job.get("sent_at") or ""),
                    "error": str(job.get("error") or ""),
                    "provider_request_id": str(
                        job.get("provider_request_id") or ""
                    ),
                    "all_tickets": all_tickets,
                    "new_tickets": new_tickets,
                }
            )
        batch_summaries.append(
            {
                "status": str(batch.get("status") or ""),
                "allocation_fingerprint": str(
                    batch.get("allocation_fingerprint")
                    or default_allocation
                ),
                "created_at": str(batch.get("created_at") or ""),
                "completed_at": str(batch.get("completed_at") or ""),
                "jobs": sorted(jobs, key=lambda item: item["email"]),
            }
        )
    return {
        "batches": batch_summaries,
        "successful_pairs": sorted([list(pair) for pair in successful]),
        "unknown_pairs": sorted([list(pair) for pair in unknown]),
    }


def canonical_snapshot(data: dict) -> dict:
    draw = ReverseDraw(data)
    source = draw.source_dataframe or {}
    source_fingerprint = str(source.get("fingerprint") or "")
    if not source_fingerprint and source.get("json"):
        source_fingerprint = hashlib.sha256(
            str(source["json"]).encode()
        ).hexdigest()
    default_allocation = (
        allocation_fingerprint(draw.owners) if draw.owners else ""
    )
    credentials = {
        person_key(key): {
            "id": str(value.get("id") or ""),
            "name": str(value.get("name") or key),
            "digest": str(value.get("digest") or ""),
            "code_scheme": str(value.get("code_scheme") or "derived-v1"),
            "created_at": str(value.get("created_at") or ""),
        }
        for key, value in draw.holder_credentials.items()
    }
    return {
        "schedule": draw.schedule,
        "tickets": {
            str(ticket): {
                "owner": draw.holder(ticket),
                "status": draw.status(ticket),
                "eliminated_round": draw.eliminated_in.get(ticket),
            }
            for ticket in draw.tickets()
        },
        "rounds": [_canonical_round(record) for record in draw.rounds],
        "undone": [_canonical_round(record) for record in draw.undone],
        "credentials": credentials,
        "source_fingerprint": source_fingerprint,
        "allocation_source_fingerprint": draw.allocation_source_fingerprint,
        "notifications": _canonical_notifications(
            draw.notification_batches, default_allocation
        ),
    }


def _compare(
    expected: Any, actual: Any, path: str, output: list[dict]
) -> None:
    if type(expected) is not type(actual):
        output.append({"path": path, "expected": expected, "actual": actual})
        return
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                output.append(
                    {
                        "path": f"{path}.{key}",
                        "expected": expected.get(key, "<missing>"),
                        "actual": actual.get(key, "<missing>"),
                    }
                )
            else:
                _compare(expected[key], actual[key], f"{path}.{key}", output)
        return
    if isinstance(expected, list):
        if len(expected) != len(actual):
            output.append(
                {
                    "path": f"{path}.length",
                    "expected": len(expected),
                    "actual": len(actual),
                }
            )
        for index, (left, right) in enumerate(zip(expected, actual)):
            _compare(left, right, f"{path}[{index}]", output)
        return
    if expected != actual:
        output.append({"path": path, "expected": expected, "actual": actual})


def reconcile(legacy_data: dict, relational_data: dict) -> dict:
    expected = canonical_snapshot(legacy_data)
    actual = canonical_snapshot(relational_data)
    differences: list[dict] = []
    _compare(expected, actual, "$", differences)
    return {
        "match": not differences,
        "difference_count": len(differences),
        "differences": differences,
        "summary": {
            "tickets": len(expected["tickets"]),
            "completed_rounds": len(expected["rounds"]),
            "undone_rounds": len(expected["undone"]),
            "credentials": len(expected["credentials"]),
            "email_batches": len(expected["notifications"]["batches"]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--draw-id", type=UUID, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", "").strip()
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    legacy_data, _ = load_export(args.export)
    with psycopg.connect(
        args.database_url, prepare_threshold=None
    ) as connection:
        relational_data = build_relational_state(connection, args.draw_id)
    report = reconcile(legacy_data, relational_data)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    if not report["match"]:
        raise SystemExit(
            "Reconciliation failed with "
            f"{report['difference_count']} differences."
        )
    print(f"Reconciliation passed for draw {args.draw_id}.")


if __name__ == "__main__":
    main()
