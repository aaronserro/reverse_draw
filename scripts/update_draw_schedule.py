"""Replace a reset relational draw schedule from app configuration."""

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
            completed_round_count = connection.execute(
                """
                SELECT count(*) FROM draw_rounds
                WHERE draw_id = %s AND status = 'completed'
                """,
                (normalized_draw_id,),
            ).fetchone()[0]
            if completed_round_count:
                raise RuntimeError(
                    "The schedule cannot be replaced while completed rounds "
                    "exist. Undo or reset the draw first."
                )

            current_stage_numbers = connection.execute(
                """
                SELECT stage_number FROM draw_stages
                WHERE draw_id = %s
                ORDER BY stage_number
                """,
                (normalized_draw_id,),
            ).fetchall()
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO draw_stages (
                        draw_id, stage_number, label, kind, prize,
                        survivor_target
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (draw_id, stage_number) DO UPDATE SET
                        label = EXCLUDED.label,
                        kind = EXCLUDED.kind,
                        prize = EXCLUDED.prize,
                        survivor_target = EXCLUDED.survivor_target
                    """,
                    stages,
                )
            extra_stage_numbers = [
                row[0]
                for row in current_stage_numbers
                if row[0] > len(stages)
            ]
            if extra_stage_numbers:
                referenced = connection.execute(
                    """
                    SELECT DISTINCT s.stage_number
                    FROM draw_stages s
                    JOIN draw_rounds r
                      ON r.stage_id = s.id AND r.draw_id = s.draw_id
                    WHERE s.draw_id = %s AND s.stage_number = ANY(%s)
                    """,
                    (normalized_draw_id, extra_stage_numbers),
                ).fetchall()
                if referenced:
                    raise RuntimeError(
                        "The configured schedule cannot remove stages that "
                        "are referenced by the draw audit history."
                    )
                connection.execute(
                    """
                    DELETE FROM draw_stages
                    WHERE draw_id = %s AND stage_number = ANY(%s)
                    """,
                    (normalized_draw_id, extra_stage_numbers),
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
