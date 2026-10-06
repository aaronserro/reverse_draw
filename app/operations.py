"""Sanitized production readiness checks and structured telemetry helpers."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("reverse_draw.operations")

_POOL_FIELDS = (
    "pool_min",
    "pool_max",
    "pool_size",
    "pool_available",
    "requests_waiting",
    "requests_num",
    "requests_errors",
    "usage_ms",
)


def request_log_record(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
    storage: str,
) -> dict[str, Any]:
    """Build a bounded telemetry record containing no user-supplied values."""
    return {
        "event": "http_request",
        "method": method.upper()[:10],
        "route": route if route.startswith("/") else "unmatched",
        "status_code": int(status_code),
        "duration_ms": round(max(0.0, duration_ms), 2),
        "storage": storage,
        "expected_version_conflict": status_code == 409,
        "transaction_failure": status_code >= 500,
    }


def _pool_report(database: Any) -> dict[str, int | bool]:
    raw = database.pool.get_stats() if database.pool is not None else {}
    report: dict[str, int | bool] = {
        key: int(raw.get(key, 0)) for key in _POOL_FIELDS
    }
    report["saturated"] = bool(report["requests_waiting"])
    return report


def readiness_report(
    database: Any,
    *,
    stale_job_seconds: int = 1800,
    maximum_query_ms: int = 2000,
) -> dict[str, Any]:
    """Return readiness invariants without exposing private data."""
    started = time.perf_counter()
    checks: dict[str, dict[str, Any]] = {}
    stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=max(1, int(stale_job_seconds))
    )
    try:
        with database.connection() as connection:
            schema = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM schema_migrations WHERE version = %s
                ) AS present
                """,
                (database.required_schema_version,),
            ).fetchone()
            schema_ok = bool(schema and schema["present"])
            checks["schema_version"] = {
                "ok": schema_ok,
                "expected": database.required_schema_version,
            }

            draw = connection.execute(
                """
                SELECT total_tickets, status, version
                FROM draws WHERE id = %s
                """,
                (database.active_draw_id,),
            ).fetchone()
            draw_ok = draw is not None
            checks["active_draw"] = {"ok": draw_ok}

            if draw_ok:
                ticket_count = connection.execute(
                    "SELECT count(*) AS count FROM tickets WHERE draw_id = %s",
                    (database.active_draw_id,),
                ).fetchone()["count"]
                expected_tickets = int(draw["total_tickets"])
                checks["ticket_count"] = {
                    "ok": ticket_count == expected_tickets,
                    "actual": int(ticket_count),
                    "expected": expected_tickets,
                }
                invalid_eliminations = connection.execute(
                    """
                    SELECT count(*) AS count
                    FROM tickets t
                    JOIN draw_rounds r ON r.id = t.eliminated_round_id
                    WHERE t.draw_id = %s AND r.status <> 'completed'
                    """,
                    (database.active_draw_id,),
                ).fetchone()["count"]
                checks["elimination_references"] = {
                    "ok": invalid_eliminations == 0,
                    "invalid_count": int(invalid_eliminations),
                }
                stale_jobs = connection.execute(
                    """
                    SELECT count(*) AS count
                    FROM email_jobs j
                    JOIN email_batches b ON b.id = j.batch_id
                    WHERE j.draw_id = %s AND j.status = 'pending'
                      AND b.status = 'sending' AND j.created_at < %s
                    """,
                    (database.active_draw_id, stale_before),
                ).fetchone()["count"]
                checks["stale_email_jobs"] = {
                    "ok": stale_jobs == 0,
                    "count": int(stale_jobs),
                    "threshold_seconds": max(1, int(stale_job_seconds)),
                }
                draw_summary = {
                    "status": draw["status"],
                    "version": int(draw["version"]),
                }
            else:
                checks["ticket_count"] = {"ok": False, "reason": "no_draw"}
                checks["elimination_references"] = {
                    "ok": False,
                    "reason": "no_draw",
                }
                checks["stale_email_jobs"] = {
                    "ok": False,
                    "reason": "no_draw",
                }
                draw_summary = None
    except Exception as error:
        log.error(
            "Operational readiness query failed (%s)",
            error.__class__.__name__,
        )
        checks["database"] = {
            "ok": False,
            "reason": "database_query_failed",
        }
        draw_summary = None

    query_duration_ms = (time.perf_counter() - started) * 1000
    pool = _pool_report(database)
    checks["pool_capacity"] = {
        "ok": not pool["saturated"],
        "requests_waiting": pool["requests_waiting"],
    }
    checks["query_latency"] = {
        "ok": query_duration_ms <= max(1, int(maximum_query_ms)),
        "duration_ms": round(query_duration_ms, 2),
        "maximum_ms": max(1, int(maximum_query_ms)),
    }
    ok = bool(checks) and all(check["ok"] for check in checks.values())
    return {
        "ok": ok,
        "storage": "supabase-relational",
        "draw_id": str(database.active_draw_id),
        "schema_version": database.required_schema_version,
        "draw": draw_summary,
        "checks": checks,
        "pool": pool,
        "query_duration_ms": round(query_duration_ms, 2),
    }


__all__ = ["readiness_report", "request_log_record"]
