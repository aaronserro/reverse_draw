"""FastAPI routes backed only by normalized relational tables."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from psycopg import OperationalError
from psycopg.errors import (
    DeadlockDetected,
    LockNotAvailable,
    QueryCanceled,
    SerializationFailure,
)
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool

from . import config
from .draw import DrawError
from .email_service import build_email_client, email_config
from .operations import readiness_report
from .projections import build_admin_payload, build_public_payload
from .projections.public_state import schedule_payload
from .repositories import (
    ConflictError,
    NotFoundError,
    Repositories,
    RepositoryError,
    ValidationError,
)
from .repositories.base import normalize_person_name
from .services.draw_service import DrawService
from .services.holder_service import CredentialProvision, HolderService
from .services.import_service import ImportService
from .services.marketplace_service import MarketplaceService
from .services.notification_service import NotificationService


SOURCE_PREVIEW_ROWS = 100


class DomainRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except NotFoundError as error:
                raise HTTPException(
                    status_code=404, detail=str(error)
                ) from error
            except ConflictError as error:
                raise HTTPException(
                    status_code=409, detail=str(error)
                ) from error
            except ValidationError as error:
                raise HTTPException(
                    status_code=400, detail=str(error)
                ) from error
            except (DeadlockDetected, SerializationFailure) as error:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The marketplace changed concurrently; refresh and "
                        "retry."
                    ),
                ) from error
            except (
                PoolTimeout,
                OperationalError,
                QueryCanceled,
                LockNotAvailable,
            ) as error:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "The database is temporarily busy. Please retry "
                        "shortly."
                    ),
                    headers={"Retry-After": "2"},
                ) from error
            except RepositoryError as error:
                raise HTTPException(
                    status_code=500, detail=str(error)
                ) from error

        return handler


@dataclass(frozen=True)
class RelationalAPIContext:
    database: Callable[[], Any]
    require_admin: Callable[[Request], None]
    require_viewer: Callable[[Request], None]
    credential_factory: Callable[[str, str], CredentialProvision]
    derive_holder_code: Callable[[str, dict[str, Any]], str]
    holder_code_digest: Callable[[str, str], str]
    sign: Callable[[str], str]
    trader_token: Callable[[dict[str, Any], int], str]
    set_trader_cookie: Callable[[Response, str, int], None]
    clear_trader_cookie: Callable[[Response], None]
    throttle: Callable[[str, int], None]
    client_ip: Callable[[Request], str]
    record_failure: Callable[[str], None]
    clear_failure: Callable[[str], None]
    parse_upload: Callable[[bytes, str], Any]
    holders_from_frame: Callable[..., Any]
    preferred_ticket_scope_applies: Callable[[Any], bool]
    dataframe_payload: Callable[[Any, dict[str, Any]], dict[str, Any]]
    source_people: Callable[[Any], dict[str, dict[str, Any]]]


class ExpectedState(BaseModel):
    expected_version: int | None = None
    expected_rounds_done: int | None = None


class ResetInput(ExpectedState):
    keep_holders: bool = True
    confirm: str


class HoldersInput(ExpectedState):
    csv: str
    mode: str = "replace"
    import_batch_id: UUID | None = None


class BlockInput(ExpectedState):
    name: str
    start: int
    end: int


class UnassignInput(ExpectedState):
    start: int | None = None
    end: int | None = None
    name: str = ""


class CredentialInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ResolveUnknownInput(BaseModel):
    batch_id: UUID
    email: str
    delivered: bool


class TraderLoginInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=20)


class ListingInput(BaseModel):
    ticket: int
    price_cents: int
    expected_draw_version: int | None = None
    expected_listing_version: int | None = None


class CancelListingInput(BaseModel):
    expected_draw_version: int | None = None
    expected_listing_version: int | None = None


class PurchaseInput(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


class ApproveInput(BaseModel):
    expected_draw_version: int | None = None


def create_relational_router(context: RelationalAPIContext) -> APIRouter:
    router = APIRouter(route_class=DomainRoute)

    def database():
        return context.database()

    def repositories(connection):
        db = database()
        return Repositories(connection, db.active_draw_id)

    def admin_dependency(request: Request) -> None:
        context.require_admin(request)

    def viewer_dependency(request: Request) -> None:
        context.require_viewer(request)

    def writable() -> None:
        if config.MAINTENANCE_MODE:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The application is temporarily read-only for maintenance."
                ),
            )

    admin = [Depends(admin_dependency)]
    admin_write = [Depends(admin_dependency), Depends(writable)]

    def version_for(body: ExpectedState, repos: Repositories) -> int:
        draw = repos.draws.get()
        if body.expected_version is not None:
            return body.expected_version
        if body.expected_rounds_done is not None:
            completed = len(repos.draws.completed_rounds())
            if completed != body.expected_rounds_done:
                raise ConflictError(
                    "The draw changed since the page loaded; refresh and "
                    "retry."
                )
        return int(draw["version"])

    def marketplace() -> MarketplaceService:
        return MarketplaceService(
            database(),
            minimum_price_cents=config.TRADING_MIN_PRICE_CENTS,
            maximum_price_cents=config.TRADING_MAX_PRICE_CENTS,
            request_ttl_seconds=config.TRADING_REQUEST_TTL_SECONDS,
        )

    def notifications() -> NotificationService:
        return NotificationService(database(), context.derive_holder_code)

    def identity(
        request: Request, *, connection: Any | None = None
    ) -> dict[str, Any] | None:
        token = request.cookies.get("rd_trader", "")
        parts = token.split(".")
        if len(parts) != 3:
            return None
        external_id, expiration, signature = parts
        if not expiration.isdigit() or int(expiration) <= time.time():
            return None

        def load_identity(active_connection):
            repos = repositories(active_connection)
            credential = repos.participants.credential_by_external_id(
                external_id
            )
            if (
                credential is None
                or not credential["active"]
                or not credential["participant_active"]
            ):
                return None
            expected = context.sign(
                "trader."
                f"{external_id}.{expiration}.{credential['code_digest']}"
            )
            if not __import__("hmac").compare_digest(signature, expected):
                return None
            return credential

        if connection is not None:
            return load_identity(connection)
        db = database()
        with db.connection() as owned_connection:
            return load_identity(owned_connection)

    def require_trader(request: Request) -> dict[str, Any]:
        current = identity(request)
        if current is None:
            raise HTTPException(status_code=401, detail="Not signed in.")
        return current

    @router.get("/healthz", include_in_schema=False)
    def health():
        report = readiness_report(
            database(),
            stale_job_seconds=config.OPERATIONAL_STALE_JOB_SECONDS,
            maximum_query_ms=config.OPERATIONAL_READINESS_MAX_QUERY_MS,
        )
        report["maintenance"] = config.MAINTENANCE_MODE
        return JSONResponse(
            report,
            status_code=200 if report["ok"] else 503,
        )

    @router.get("/livez", include_in_schema=False)
    def live():
        return {"ok": True}

    @router.get("/api/config")
    def get_config():
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            draw = repos.draws.get()
            stages = repos.draws.stages()
        return {
            "org_name": config.ORG_NAME,
            "org_initials": config.ORG_INITIALS
            or "".join(
                word[0] for word in config.ORG_NAME.split()[:2]
            ).upper(),
            "posted_in": config.POSTED_IN,
            "prize_text": config.PRIZE_TEXT,
            "closing_note": config.CLOSING_NOTE,
            "schedule": schedule_payload(draw, stages),
            "grid_columns": config.GRID_COLUMNS,
            "highlight_last_round": config.HIGHLIGHT_LAST_ROUND,
            "show_holder_names": config.PUBLIC_SHOW_HOLDER_NAMES,
            "refresh_seconds": config.PUBLIC_REFRESH_SECONDS,
            "trading_enabled": config.TRADING_ENABLED,
            "trading_poll_seconds": config.TRADING_POLL_SECONDS,
            "trading_poll_jitter_percent": (
                config.TRADING_POLL_JITTER_PERCENT
            ),
            "trading_poll_max_backoff_seconds": (
                config.TRADING_POLL_MAX_BACKOFF_SECONDS
            ),
            "trading_min_price_cents": config.TRADING_MIN_PRICE_CENTS,
            "trading_max_price_cents": config.TRADING_MAX_PRICE_CENTS,
            "colors": {
                "active": config.COLOR_ACTIVE,
                "eliminated": config.COLOR_ELIMINATED,
                "last_round": config.COLOR_LAST_ROUND,
                "winner": config.COLOR_WINNER,
                "prize": config.COLOR_PRIZE,
                "highlight": config.COLOR_HIGHLIGHT,
            },
        }

    @router.get("/api/state", dependencies=[Depends(viewer_dependency)])
    def public_state():
        db = database()
        with db.connection() as connection:
            return build_public_payload(
                repositories(connection),
                show_holder_names=config.PUBLIC_SHOW_HOLDER_NAMES,
                show_winner_name=config.PUBLIC_SHOW_WINNER_NAME,
            )

    @router.get("/api/admin/state", dependencies=admin)
    def admin_state():
        db = database()
        with db.connection() as connection:
            return build_admin_payload(
                repositories(connection),
                derive_code=context.derive_holder_code,
                public_code_set=bool(
                    __import__("os").getenv("PUBLIC_ACCESS_CODE")
                    or config.PUBLIC_ACCESS_CODE
                ),
            )

    @router.post("/api/admin/rounds/next", dependencies=admin_write)
    def run_round(body: ExpectedState):
        db = database()
        with db.connection() as connection:
            expected = version_for(body, repositories(connection))
        return DrawService(db).run_next_round(expected)

    @router.post("/api/admin/rounds/undo", dependencies=admin_write)
    def undo_round(body: ExpectedState):
        db = database()
        with db.connection() as connection:
            expected = version_for(body, repositories(connection))
        return DrawService(db).undo_last_round(
            expected, admin_identifier="administrator"
        )

    @router.post("/api/admin/reset", dependencies=admin_write)
    def reset_draw(body: ResetInput):
        if body.confirm != "RESET":
            raise ValidationError("Type RESET to confirm.")
        db = database()
        with db.connection() as connection:
            expected = version_for(body, repositories(connection))
        return DrawService(db).reset_draw(
            expected,
            keep_holders=body.keep_holders,
            admin_identifier="administrator",
        )

    @router.put("/api/admin/holders", dependencies=admin_write)
    def put_holders(body: HoldersInput):
        mapping = _parse_holder_csv(body.csv)
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            expected = version_for(body, repos)
            matching_batch = body.import_batch_id
            candidate = None
            if matching_batch is not None:
                candidate = repos.imports.get_batch(matching_batch)
            else:
                latest = repos.imports.latest()
                if latest and latest["status"] == "previewed":
                    candidate = latest
            if candidate is not None and candidate["status"] == "previewed":
                rows = repos.imports.rows(candidate["id"])
                preview_mapping = {
                    int(row["ticket_number"]): row["holder_name"]
                    for row in rows
                    if row["ticket_number"] is not None
                    and row["holder_name"]
                }
                if preview_mapping == mapping:
                    matching_batch = candidate["id"]
                elif body.import_batch_id is not None:
                    raise ConflictError(
                        "The allocation was edited after preview. "
                        "Upload the file again or save it as a manual list."
                    )
        if matching_batch is not None:
            payload = ImportService(
                db, context.credential_factory
            ).apply_batch(
                matching_batch,
                mode=body.mode,
                expected_version=expected,
                actor_identifier="administrator",
            )
        else:
            payload = HolderService(
                db, context.credential_factory
            ).apply_allocation(
                mapping,
                mode=body.mode,
                expected_version=expected,
                actor_identifier="administrator",
            )
        payload["imported"] = len(mapping)
        payload.setdefault("new_trading_credentials", [])
        return payload

    @router.post("/api/admin/holders/file", dependencies=admin_write)
    async def upload_holders(
        request: Request,
        filename: str,
        preferred_ticket_scope: str = "first_100",
    ):
        data = await request.body()
        if not data:
            raise ValidationError("The selected file is empty.")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Files must be 10 MB or smaller.")
        db = database()
        scope = preferred_ticket_scope.strip().casefold()
        if scope not in {"first_100", "all"}:
            raise ValidationError(
                "Preferred ticket scope must be first_100 or all."
            )

        def prepare_upload() -> dict[str, Any]:
            with db.connection() as connection:
                draw = repositories(connection).draws.get()
            source = context.parse_upload(data, filename)
            preference_scope_applies = (
                context.preferred_ticket_scope_applies(source)
            )
            holders = context.holders_from_frame(
                source,
                int(draw["total_tickets"]),
                preferred_ticket_scope=scope,
            )
            source_json = source.to_json(orient="split", date_format="iso")
            source_fingerprint = hashlib.sha256(
                source_json.encode()
            ).hexdigest()
            people = context.source_people(source)
            metadata = {
                "filename": filename,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            with db.transaction() as connection:
                repos = repositories(connection)
                batch = repos.imports.create_batch(
                    filename=filename,
                    source_fingerprint=source_fingerprint,
                )
                rows = []
                for row_number, row in enumerate(
                    holders.itertuples(index=False), start=1
                ):
                    name = str(row.name)
                    key = normalize_person_name(name)
                    emails = people.get(key, {}).get("emails", set())
                    rows.append(
                        {
                            "row_number": row_number,
                            "ticket_number": int(row.ticket),
                            "holder_name": name,
                            "normalized_holder_name": key,
                            "email": (
                                next(iter(emails))
                                if len(emails) == 1
                                else None
                            ),
                            "raw_data": {
                                "ticket": int(row.ticket),
                                "name": name,
                                "source_fingerprint": source_fingerprint,
                                "preferred_ticket_scope": scope,
                            },
                            "validation_error": (
                                "Conflicting email addresses for participant."
                                if len(emails) > 1
                                else None
                            ),
                        }
                    )
                repos.imports.add_rows(batch["id"], rows)
            grouped = holders.groupby("name", sort=True)["ticket"].agg(list)
            dataframe = context.dataframe_payload(source, metadata)
            dataframe["rows"] = dataframe["rows"][:SOURCE_PREVIEW_ROWS]
            dataframe["shown_count"] = len(dataframe["rows"])
            dataframe["truncated"] = (
                dataframe["row_count"] > dataframe["shown_count"]
            )
            return {
                "batch_id": str(batch["id"]),
                "csv": holders.to_csv(index=False),
                "imported": len(holders),
                "preferred_ticket_scope": scope,
                "preferred_ticket_scope_applies": preference_scope_applies,
                "dataframe": dataframe,
                "people": [
                    {"name": name, "tickets": tickets}
                    for name, tickets in grouped.items()
                ],
            }

        try:
            return await run_in_threadpool(prepare_upload)
        except (DrawError, ValueError, KeyError, OSError) as error:
            raise ValidationError(
                f"Could not read {filename}: {error}"
            ) from error

    @router.get("/api/admin/holders/dataframe", dependencies=admin)
    def holder_dataframe():
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            batch = repos.imports.latest()
            if batch is None:
                return {"dataframe": None}
            rows = repos.imports.rows(batch["id"])
        columns = ["ticket", "name", "email"]
        preview_rows = rows[:SOURCE_PREVIEW_ROWS]
        return {
            "dataframe": {
                "filename": batch["filename"],
                "uploaded_at": batch["uploaded_at"],
                "columns": columns,
                "rows": [
                    [
                        row["ticket_number"],
                        row["holder_name"],
                        row["email"],
                    ]
                    for row in preview_rows
                ],
                "row_count": len(rows),
                "shown_count": len(preview_rows),
                "truncated": len(rows) > len(preview_rows),
                "column_count": len(columns),
            }
        }

    @router.post("/api/admin/holders/block", dependencies=admin_write)
    def assign_block(body: BlockInput):
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            expected = version_for(body, repos)
            total = int(repos.draws.get()["total_tickets"])
            lower, upper = sorted((body.start, body.end))
            lower, upper = max(lower, 1), min(upper, total)
        payload = HolderService(db, context.credential_factory).assign_block(
            body.name,
            body.start,
            body.end,
            expected_version=expected,
            actor_identifier="administrator",
        )
        payload["assigned"] = max(0, upper - lower + 1)
        payload.setdefault("new_trading_credentials", [])
        return payload

    @router.post("/api/admin/holders/unassign", dependencies=admin_write)
    def unassign(body: UnassignInput):
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            expected = version_for(body, repos)
            if body.name.strip():
                participant = repos.participants.by_name(body.name)
                if participant is None:
                    raise NotFoundError("Ticket holder not found.")
                numbers = [
                    row["ticket_number"]
                    for row in repos.tickets.for_participant(participant["id"])
                ]
            elif body.start is not None:
                end = body.end if body.end is not None else body.start
                lower, upper = sorted((body.start, end))
                numbers = [
                    row["ticket_number"]
                    for row in repos.tickets.all()
                    if lower <= row["ticket_number"] <= upper
                    and row["owner_participant_id"] is not None
                ]
            else:
                raise ValidationError("Enter a holder name or ticket range.")
        if not numbers:
            payload = admin_state()
            payload["unassigned"] = 0
            return payload
        service = HolderService(db, context.credential_factory)
        payload = None
        for lower, upper in _contiguous_ranges(numbers):
            payload = service.unassign_block(
                lower,
                upper,
                expected_version=expected,
                actor_identifier="administrator",
            )
            expected = int(payload["version"])
        payload["unassigned"] = len(numbers)
        return payload

    @router.get("/api/admin/notifications/preview", dependencies=admin)
    def notification_preview():
        return notifications().preview(email_config())

    def process_batch(batch_id: UUID) -> None:
        service = notifications()
        client = build_email_client(email_config())
        db = database()
        with db.connection() as connection:
            jobs = repositories(connection).notifications.jobs(batch_id)
        for job in jobs:
            service.process_job(job["id"], client)

    @router.post(
        "/api/admin/notifications/send-all", dependencies=admin_write
    )
    def send_notifications(background_tasks: BackgroundTasks):
        result = notifications().create_batch(email_config())
        background_tasks.add_task(process_batch, result["batch"]["id"])
        return result

    @router.post("/api/admin/notifications/cancel", dependencies=admin_write)
    def cancel_notifications():
        db = database()
        now = datetime.now(timezone.utc)
        with db.transaction() as connection:
            repos = repositories(connection)
            batch = repos.notifications.active_batch(lock=True)
            if batch is None:
                raise ConflictError("No email batch is sending.")
            connection.execute(
                """
                UPDATE email_jobs SET status = 'cancelled', error = %s
                WHERE draw_id = %s AND batch_id = %s AND status = 'pending'
                """,
                (
                    "Canceled by the administrator before sending.",
                    db.active_draw_id,
                    batch["id"],
                ),
            )
            connection.execute(
                """
                UPDATE email_batches
                SET status = 'cancelled', completed_at = %s
                WHERE id = %s
                """,
                (now, batch["id"]),
            )
        return notifications().preview(email_config())

    @router.post(
        "/api/admin/notifications/clear-history", dependencies=admin_write
    )
    def clear_notification_history():
        db = database()
        with db.transaction() as connection:
            repos = repositories(connection)
            if repos.notifications.active_batch(lock=True) is not None:
                raise ConflictError(
                    "Cancel the sending batch before clearing history."
                )
            result = connection.execute(
                "DELETE FROM email_batches WHERE draw_id = %s",
                (db.active_draw_id,),
            )
        return {
            "cleared_batches": result.rowcount,
            "preview": notifications().preview(email_config()),
        }

    @router.post(
        "/api/admin/notifications/resolve-unknown", dependencies=admin_write
    )
    def resolve_unknown(body: ResolveUnknownInput):
        db = database()
        status = "sent" if body.delivered else "failed"
        now = datetime.now(timezone.utc)
        with db.transaction() as connection:
            job = connection.execute(
                """
                SELECT * FROM email_jobs
                WHERE draw_id = %s AND batch_id = %s
                  AND lower(recipient_email::text) = lower(%s)
                FOR UPDATE
                """,
                (db.active_draw_id, body.batch_id, body.email),
            ).fetchone()
            if job is None:
                raise NotFoundError("Email job not found.")
            if job["status"] != "unknown":
                raise ConflictError(
                    "The delivery result is no longer unknown."
                )
            connection.execute(
                """
                UPDATE email_jobs
                SET status = %s, sent_at = %s, error = %s
                WHERE id = %s
                """,
                (
                    status,
                    now if body.delivered else None,
                    "Manually resolved by the administrator.",
                    job["id"],
                ),
            )
            repositories(connection).notifications.refresh_batch_status(
                body.batch_id
            )
        return notifications().preview(email_config())

    @router.post(
        "/api/admin/trading/credentials/generate", dependencies=admin_write
    )
    def generate_credentials():
        db = database()
        generated = []
        with db.transaction() as connection:
            repos = repositories(connection)
            repos.draws.lock()
            service = HolderService(db, context.credential_factory)
            for participant in repos.participants.list():
                _, created = service._participant_for(
                    repos, participant["display_name"]
                )
                generated.extend(created)
            repos.draws.increment_version()
            payload = build_admin_payload(
                repos, derive_code=context.derive_holder_code
            )
        payload["new_trading_credentials"] = generated
        return payload

    @router.post(
        "/api/admin/trading/credentials/reset", dependencies=admin_write
    )
    def reset_credential(body: CredentialInput):
        db = database()
        with db.transaction() as connection:
            repos = repositories(connection)
            repos.draws.lock()
            participant = repos.participants.by_name(body.name)
            if participant is None:
                raise NotFoundError("Ticket holder not found.")
            provision = context.credential_factory(
                participant["normalized_name"], participant["display_name"]
            )
            existing = repos.participants.credential_for_participant(
                participant["id"]
            )
            if existing is None:
                repos.participants.create_credential(
                    participant["id"],
                    external_id=provision.external_id,
                    digest=provision.digest,
                    scheme=provision.scheme,
                )
            else:
                repos.participants.rotate_credential(
                    participant["id"],
                    external_id=provision.external_id,
                    digest=provision.digest,
                    scheme=provision.scheme,
                )
            repos.draws.increment_version()
            payload = build_admin_payload(
                repos, derive_code=context.derive_holder_code
            )
        payload["new_trading_credentials"] = [
            {
                "name": participant["display_name"],
                "code": provision.readable_code,
            }
        ]
        return payload

    @router.get("/api/trading/session")
    def trading_session(request: Request):
        current = identity(request)
        if current is None:
            return {"authenticated": False}
        if config.TRADING_ENABLED:
            return marketplace().snapshot(current["participant_id"])
        db = database()
        with db.connection() as connection:
            from .projections.trader_state import build_trader_payload

            return build_trader_payload(
                repositories(connection), current["participant_id"]
            )

    @router.post("/api/trading/login")
    def trading_login(
        body: TraderLoginInput, request: Request, response: Response
    ):
        key = normalize_person_name(body.name)
        code = body.code.strip()
        ip_key = f"trader-ip:{context.client_ip(request)}"
        holder_key = (
            f"trader-holder:{context.client_ip(request)}:"
            f"{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        )
        context.throttle(ip_key, 20)
        context.throttle(holder_key, 5)
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            participant = repos.participants.by_name(key)
            credential = (
                repos.participants.credential_for_participant(
                    participant["id"]
                )
                if participant is not None
                else None
            )
        expected = (
            credential["code_digest"] if credential is not None else "0" * 64
        )
        supplied = context.holder_code_digest(key, code)
        valid = bool(
            re.fullmatch(r"\d{6}", code)
            and participant is not None
            and participant["active"]
            and credential is not None
            and credential["active"]
            and __import__("hmac").compare_digest(supplied, expected)
        )
        if not valid:
            context.record_failure(ip_key)
            context.record_failure(holder_key)
            raise HTTPException(
                401, "The name or six-digit access code is incorrect."
            )
        context.clear_failure(holder_key)
        seconds = int(config.TRADER_SESSION_DAYS * 86400)
        token = context.trader_token(credential, seconds)
        context.set_trader_cookie(response, token, seconds)
        if config.TRADING_ENABLED:
            return marketplace().snapshot(participant["id"])
        with db.connection() as connection:
            from .projections.trader_state import build_trader_payload

            return build_trader_payload(
                repositories(connection), participant["id"]
            )

    @router.get("/api/trading/market")
    def trading_market(request: Request, response: Response):
        _require_trading_enabled()
        db = database()
        with db.connection() as connection:
            current = identity(request, connection=connection)
            if current is None:
                raise HTTPException(status_code=401, detail="Not signed in.")
            draw = repositories(connection).draws.get()
            etag = (
                f'"{draw["version"]}-{draw["marketplace_version"]}-'
                f'{current["participant_id"]}"'
            )
            if request.headers.get("if-none-match") == etag:
                return Response(
                    status_code=304,
                    headers={
                        "ETag": etag,
                        "Cache-Control": "private, no-cache",
                    },
                )
            payload = marketplace().snapshot(
                current["participant_id"], connection=connection
            )
        response.headers["ETag"] = etag
        return payload

    @router.post("/api/trading/listings", dependencies=[Depends(writable)])
    def upsert_listing(body: ListingInput, request: Request):
        current = require_trader(request)
        _require_trading_enabled()
        listing = marketplace().upsert_listing(
            current["participant_id"],
            body.ticket,
            body.price_cents,
            expected_draw_version=body.expected_draw_version,
            expected_listing_version=body.expected_listing_version,
        )
        return {
            "listing_id": str(listing["id"]),
            "version": listing["version"],
        }

    @router.delete(
        "/api/trading/listings/{listing_id}",
        dependencies=[Depends(writable)],
    )
    def cancel_listing(
        listing_id: UUID, body: CancelListingInput, request: Request
    ):
        current = require_trader(request)
        _require_trading_enabled()
        return marketplace().cancel_listing(
            current["participant_id"],
            listing_id,
            expected_draw_version=body.expected_draw_version,
            expected_listing_version=body.expected_listing_version,
        )

    @router.post(
        "/api/trading/listings/{listing_id}/requests",
        dependencies=[Depends(writable)],
    )
    def request_purchase(
        listing_id: UUID, body: PurchaseInput, request: Request
    ):
        current = require_trader(request)
        _require_trading_enabled()
        row = marketplace().request_purchase(
            current["participant_id"],
            listing_id,
            idempotency_key=body.idempotency_key,
        )
        return {"request_id": str(row["id"]), "status": row["status"]}

    @router.post(
        "/api/trading/requests/{request_id}/approve",
        dependencies=[Depends(writable)],
    )
    def approve_request(
        request_id: UUID, body: ApproveInput, request: Request
    ):
        current = require_trader(request)
        _require_trading_enabled()
        return marketplace().approve_request(
            current["participant_id"],
            request_id,
            expected_draw_version=body.expected_draw_version,
        )

    @router.post(
        "/api/trading/requests/{request_id}/decline",
        dependencies=[Depends(writable)],
    )
    def decline_request(request_id: UUID, request: Request):
        current = require_trader(request)
        _require_trading_enabled()
        row = marketplace().decline_request(
            current["participant_id"], request_id
        )
        return {"request_id": str(row["id"]), "status": row["status"]}

    @router.post(
        "/api/trading/requests/{request_id}/withdraw",
        dependencies=[Depends(writable)],
    )
    def withdraw_request(request_id: UUID, request: Request):
        current = require_trader(request)
        _require_trading_enabled()
        row = marketplace().withdraw_request(
            current["participant_id"], request_id
        )
        return {"request_id": str(row["id"]), "status": row["status"]}

    def csv_response(text: str, filename: str) -> PlainTextResponse:
        return PlainTextResponse(
            text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @router.get("/api/admin/export/holders.csv", dependencies=admin)
    def export_holders():
        db = database()
        with db.connection() as connection:
            owners = repositories(connection).tickets.owner_map()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ticket", "name"])
        writer.writerows(sorted(owners.items()))
        return csv_response(output.getvalue(), "ticket_holders.csv")

    @router.get("/api/admin/export/tickets.csv", dependencies=admin)
    def export_tickets():
        db = database()
        with db.connection() as connection:
            rows = repositories(connection).tickets.all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Ticket", "Holder", "Status", "Eliminated round"])
        for row in rows:
            status = (
                f"Eliminated in {row['eliminated_label']}"
                if row["eliminated_round_id"]
                else "Still in"
            )
            writer.writerow(
                [
                    row["ticket_number"],
                    row["owner_name"] or "",
                    status,
                    row["eliminated_label"] or "",
                ]
            )
        return csv_response(output.getvalue(), "ticket_status.csv")

    @router.get("/api/admin/export/log.csv", dependencies=admin)
    def export_log():
        db = database()
        with db.connection() as connection:
            repos = repositories(connection)
            rounds = repos.draws.completed_rounds()
            owners = repos.tickets.owner_map()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Round",
                "Round label",
                "Timestamp (UTC)",
                "Seed",
                "Ticket",
                "Holder",
            ]
        )
        for row in rounds:
            for ticket in row["result_tickets"]:
                writer.writerow(
                    [
                        row["round_number"],
                        row["label_snapshot"],
                        row["executed_at"],
                        row["seed"],
                        ticket,
                        owners.get(ticket, ""),
                    ]
                )
        return csv_response(output.getvalue(), "elimination_log.csv")

    return router


def install_relational_router(app: Any, router: APIRouter) -> None:
    replacements = {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and any(
                (route.path, method) in replacements
                for method in route.methods
            )
        )
    ]
    app.include_router(router)


def _parse_holder_csv(text: str) -> dict[int, str]:
    mapping = {}
    try:
        rows = csv.reader(io.StringIO(text))
        for row in rows:
            if len(row) < 2:
                continue
            try:
                ticket = int(row[0].strip())
            except ValueError:
                continue
            name = ",".join(row[1:]).strip()
            if name:
                mapping[ticket] = name
    except csv.Error as error:
        raise ValidationError(
            f"Could not read holder rows: {error}"
        ) from error
    if not mapping:
        raise ValidationError("No valid ticket-holder rows were found.")
    return mapping


def _require_trading_enabled() -> None:
    if not config.TRADING_ENABLED:
        raise HTTPException(status_code=503, detail="Trading is not enabled.")


def _contiguous_ranges(numbers: list[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(numbers))
    if not ordered:
        return []
    ranges = []
    lower = upper = ordered[0]
    for number in ordered[1:]:
        if number == upper + 1:
            upper = number
            continue
        ranges.append((lower, upper))
        lower = upper = number
    ranges.append((lower, upper))
    return ranges
