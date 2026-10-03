"""Run normalized database readiness checks for deployment automation."""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from app.db import RelationalDatabase
from app.operations import readiness_report


def run_check(
    database_url: str,
    draw_id: str,
    *,
    stale_job_seconds: int = 1800,
) -> dict:
    database = None
    try:
        database = RelationalDatabase(database_url, draw_id)
        return readiness_report(
            database,
            stale_job_seconds=stale_job_seconds,
        )
    except Exception as error:
        return {
            "ok": False,
            "storage": "supabase-relational",
            "checks": {
                "startup": {
                    "ok": False,
                    "reason": "readiness_initialization_failed",
                    "error_type": error.__class__.__name__,
                }
            },
        }
    finally:
        if database is not None:
            database.close()


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
    parser.add_argument(
        "--stale-job-seconds",
        type=int,
        default=int(os.getenv("OPERATIONAL_STALE_JOB_SECONDS", "1800")),
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if not args.draw_id:
        parser.error("--draw-id or ACTIVE_DRAW_ID is required")
    report = run_check(
        args.database_url,
        args.draw_id,
        stale_job_seconds=args.stale_job_seconds,
    )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
