"""Export normalized relational state in the legacy JSON document format."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg

from scripts.migration_common import source_checksum
from scripts.reconcile_legacy_state import build_relational_state


def reverse_export(
    connection: psycopg.Connection,
    draw_id: UUID,
    output: Path,
) -> dict:
    state = build_relational_state(connection, draw_id)
    document = {
        "exported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source": "supabase-relational",
        "draw_id": str(draw_id),
        "canonical_state_sha256": source_checksum(state),
        "data": state,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draw-id", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", "").strip()
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    with psycopg.connect(
        args.database_url, prepare_threshold=None
    ) as connection:
        document = reverse_export(connection, args.draw_id, args.output)
    print(
        f"Exported relational draw {args.draw_id} with checksum "
        f"{document['canonical_state_sha256']}."
    )


if __name__ == "__main__":
    main()
