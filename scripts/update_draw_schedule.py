"""Replace an unstarted relational draw schedule from app configuration."""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence
from uuid import UUID

import psycopg

from app.draw import current_schedule, validate_schedule

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


CONFIRMATION = "UPDATE-SCHEDULE"


def update_schedule(database_url: str, draw_id: str) -> dict:
    schedule = current_schedule()
    validate_schedule(schedule)
    normalized_draw_id = UUID(draw_id)
    stages = [
        (
            normalized_draw_id,
            index,
            label,
            kind,
            prize,
            survivor_target,
        )
        for index, (label, kind, prize, survivor_target) in enumerate(
            zip(
                schedule["labels"],
                schedule["kinds"],
                schedule["prizes"],
                schedule["survivors"],
                strict=True,
            ),
            start=1,
        )
    ]

    with psycopg.connect(database_url, prepare_threshold=None) as connection:
        with connection.transaction():
            draw = connection.execute(
                "SELECT id, total_tickets FROM draws WHERE id = %s FOR UPDATE",
                (normalized_draw_id,),
            ).fetchone()
            if draw is None:
                raise RuntimeError(
                    f"Draw {normalized_draw_id} does not exist."
                )
            if int(draw[1]) != int(schedule["total"]):
                raise RuntimeError(
                    "The configured ticket total does not match the draw."
                )
            round_count = connection.execute(
                "SELECT count(*) FROM draw_rounds WHERE draw_id = %s",
                (normalized_draw_id,),
            ).fetchone()[0]
            if round_count:
                raise RuntimeError(
                    "The schedule cannot be replaced after any round has "
                    "been recorded."
                )

            connection.execute(
                "DELETE FROM draw_stages WHERE draw_id = %s",
                (normalized_draw_id,),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO draw_stages (
                        draw_id, stage_number, label, kind, prize,
                        survivor_target
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    stages,
                )
            connection.execute(
                """
                UPDATE draws
                SET completion_label = %s,
                    completion_label_plural = %s,
                    version = version + 1
                WHERE id = %s
                """,
                (
                    schedule["completion_label"],
                    schedule["completion_label_plural"],
                    normalized_draw_id,
                ),
            )

    return {
        "draw_id": str(normalized_draw_id),
        "stages": [
            {
                "stage_number": stage[1],
                "label": stage[2],
                "kind": stage[3],
                "prize": stage[4],
                "survivor_target": stage[5],
            }
            for stage in stages
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "").strip(),
    )
    parser.add_argument(
        "--draw-id",
        default=os.getenv("ACTIVE_DRAW_ID", "").strip(),
    )
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if not args.draw_id:
        parser.error("--draw-id or ACTIVE_DRAW_ID is required")
    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm {CONFIRMATION} is required")
    result = update_schedule(args.database_url, args.draw_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
