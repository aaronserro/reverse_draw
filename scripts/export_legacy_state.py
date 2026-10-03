"""Export the singleton JSON state and independent verification artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from scripts.migration_common import canonical_json, validate_legacy_document


def _write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def export_legacy_state(connection: psycopg.Connection, output: Path) -> dict:
    row = connection.execute(
        "SELECT data, updated_at FROM draw_state WHERE id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Legacy draw_state row 1 does not exist.")

    data, updated_at = row
    draw = validate_legacy_document(data)
    output.mkdir(parents=True, exist_ok=True)

    document = {
        "exported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "legacy_updated_at": updated_at.isoformat() if updated_at else None,
        "data": data,
    }
    _write_text(
        output / "draw_state.json",
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
    )
    _write_text(output / "holders.csv", draw.owners_csv())
    _write_text(output / "tickets.csv", draw.tickets_csv())
    _write_text(output / "round-log.csv", draw.log_csv())

    baseline = [
        {
            "ticket": ticket,
            "holder": draw.holder(ticket),
            "status": draw.status(ticket),
            "eliminated_round": draw.eliminated_in.get(ticket),
        }
        for ticket in draw.tickets()
    ]
    _write_text(
        output / "ticket-baseline.json",
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
    )

    manifest = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "checksums.json":
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["canonical_state_sha256"] = hashlib.sha256(
        canonical_json(data).encode()
    ).hexdigest()
    _write_text(
        output / "checksums.json",
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", "").strip()
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    with psycopg.connect(
        args.database_url, prepare_threshold=None
    ) as connection:
        manifest = export_legacy_state(connection, args.output)
    print(f"Exported {len(manifest) - 1} artifacts to {args.output}.")


if __name__ == "__main__":
    main()
