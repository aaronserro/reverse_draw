"""
Reverse Draw - FastAPI app.

Public:  GET /             board page (asks for the 6-digit access code first)
         POST /api/access  check the access code
         GET /api/config   display settings (no secrets)
         GET /api/state    current draw (needs the access code)
Admin:   GET /admin        control page (password login)
         /api/admin/...    run/undo/reset rounds, manage holders, CSV exports
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:  # load a local .env file if present (not needed on Render)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from . import config
from .db import make_relational_database, make_store
from .draw import DrawError, ReverseDraw, current_schedule, now_iso
from .email_service import EmailSendError, build_email_client, email_config
from .operations import request_log_record

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reverse_draw")

STATIC = Path(__file__).parent / "static"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
PUBLIC_CODE = (os.getenv("PUBLIC_ACCESS_CODE") or str(config.PUBLIC_ACCESS_CODE or "")).strip()
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
ON_RENDER = bool(os.getenv("RENDER"))
ADMIN_COOKIE = "rd_admin"
PUBLIC_COOKIE = "rd_public"
TRADER_COOKIE = "rd_trader"

store = None
relational_database = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global relational_database, store
    if config.RELATIONAL_STORE_ENABLED:
        relational_database = make_relational_database()
        store = None
        log.info(
            "Using normalized relational storage for draw %s",
            relational_database.active_draw_id,
        )
    else:
        store = make_store()
        if (
            ON_RENDER
            and config.REQUIRE_DATABASE_IN_PRODUCTION
            and store.kind == "sqlite"
        ):
            store.close()
            store = None
            raise RuntimeError(
                "DATABASE_URL is required in production; refusing SQLite."
            )
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set - the admin page is disabled.")
    if PUBLIC_CODE and not re.fullmatch(r"\d{6}", PUBLIC_CODE):
        log.warning("PUBLIC_ACCESS_CODE should be exactly 6 digits (got %d characters).", len(PUBLIC_CODE))
    if not os.getenv("SECRET_KEY"):
        log.warning("SECRET_KEY is not set - logins will reset whenever the server restarts.")
    if store is not None and ON_RENDER and store.kind == "sqlite":
        log.error("Running on Render without DATABASE_URL - the draw will be LOST on every restart.")
    if store is not None:
        with Mutation() as draw:
            _sync_holder_credentials(draw)
    try:
        yield
    finally:
        if relational_database is not None:
            relational_database.close()
            relational_database = None
        if store is not None:
            store.close()
            store = None


app = FastAPI(title="Reverse Draw", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def prevent_api_caching(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        storage = (
            "supabase-relational"
            if config.RELATIONAL_STORE_ENABLED
            else getattr(store, "kind", "initializing")
        )
        record = request_log_record(
            method=request.method,
            route=route,
            status_code=status_code,
            duration_ms=(time.perf_counter() - started) * 1000,
            storage=storage,
        )
        log.info(json.dumps(record, separators=(",", ":")))


# =============================================================================
# Helpers
# =============================================================================
def load() -> ReverseDraw:
    return ReverseDraw(store.read())


class Mutation:
    """`with Mutation() as draw:` - locks the row, yields the draw, saves on exit."""

    def __enter__(self) -> ReverseDraw:
        self._cm = store.transaction()
        self._box = self._cm.__enter__()
        self.draw = ReverseDraw(self._box.data)
        return self.draw

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._box.data = self.draw.to_dict()
        result = self._cm.__exit__(exc_type, exc, tb)
        _public_cache.clear()
        return result


# Small cache so hundreds of people polling the public page don't each hit the DB.
_public_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 2.0


def version_of(d: ReverseDraw) -> str:
    return hashlib.sha1(json.dumps(d.to_dict(), sort_keys=True).encode()).hexdigest()[:16]


def round_summary(rec: dict) -> dict:
    return {
        "round": rec["round"],
        "label": rec["label"],
        "kind": rec.get("kind", "elimination"),
        "prize": rec.get("prize", ""),
        "selected_tickets": (
            rec["eliminated"] if rec.get("kind") == "prize" else []
        ),
        "timestamp": rec["timestamp"],
        "started_with": rec["started_with"],
        "survivors": rec["survivors"],
        "eliminated_count": len(rec["eliminated"]),
    }


def public_payload(d: ReverseDraw) -> dict:
    show_names = config.PUBLIC_SHOW_HOLDER_NAMES
    return {
        "version": version_of(d),
        "schedule": d.schedule,
        "started": d.started,
        "finished": d.finished,
        "rounds_done": d.rounds_done,
        "next_label": None if d.finished else d.labels[d.rounds_done],
        "status": d.status_array(),
        "rounds": [round_summary(r) for r in d.rounds],
        "winners": [
            {"ticket": t, "holder": d.holder(t) if (show_names or config.PUBLIC_SHOW_WINNER_NAME) else ""}
            for t in d.winners()
        ],
        "holders": {str(t): n for t, n in d.owners.items()} if show_names else None,
    }


def admin_payload(d: ReverseDraw) -> dict:
    out = public_payload(d)
    summary = d.holder_summary()
    for person in summary:
        holder_key = _person_key(person["holder"])
        credential = d.holder_credentials.get(holder_key)
        person["trading_ready"] = credential is not None
        person["trading_code"] = (
            _derive_holder_code(holder_key, credential)
            if credential is not None
            else ""
        )
    out.update(
        holders={str(t): n for t, n in d.owners.items()},
        rounds=[{**round_summary(r), "seed": r["seed"], "eliminated": r["eliminated"]} for r in d.rounds],
        undone=[
            {**round_summary(r), "seed": r["seed"], "undone_at": r["undone_at"], "eliminated": r["eliminated"]}
            for r in d.undone
        ],
        summary=summary,
        storage=store.kind,
        warn_no_db=ON_RENDER and store.kind == "sqlite",
        schedule_pending=d.schedule_pending,
        public_code_set=bool(PUBLIC_CODE),
    )
    return out


def bad_request(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Sessions (signed HttpOnly cookies)
# =============================================================================
def _sign(msg: str) -> str:
    return hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token(kind: str, seconds: int, bind: str = "") -> str:
    exp = int(time.time() + seconds)
    return f"{exp}.{_sign(f'{kind}.{exp}.{bind}')}"


def token_ok(token: str | None, kind: str, bind: str = "") -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(f"{kind}.{exp}.{bind}"))


def _code_fingerprint() -> str:
    # Changing the access code signs everyone out.
    return hashlib.sha256(PUBLIC_CODE.encode()).hexdigest()[:12]


def is_admin(request: Request) -> bool:
    return token_ok(request.cookies.get(ADMIN_COOKIE), "admin")


def is_viewer(request: Request) -> bool:
    if not PUBLIC_CODE or is_admin(request):
        return True
    return token_ok(request.cookies.get(PUBLIC_COOKIE), "public", _code_fingerprint())


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Not logged in.")


def require_viewer(request: Request) -> None:
    if not is_viewer(request):
        raise HTTPException(status_code=401, detail="Access code required.")


def set_cookie(response: Response, name: str, value: str, seconds: int) -> None:
    response.set_cookie(name, value, max_age=seconds, httponly=True, secure=ON_RENDER, samesite="lax")


def _holder_code_digest(holder_key: str, code: str) -> str:
    return _sign(f"holder-code.{holder_key}.{code}")


def _derive_holder_code(holder_key: str, credential: dict) -> str:
    """Reproduce a credential's code without persisting the readable value."""
    credential_id = str(
        credential.get("credential_external_id") or credential["id"]
    )
    digest = hmac.new(
        SECRET_KEY.encode(),
        f"holder-code-v1.{holder_key}.{credential_id}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


def _active_holder_names(draw: ReverseDraw) -> dict[str, str]:
    return {
        _person_key(name): name
        for name in draw.owners.values()
        if _person_key(name)
    }


def _sync_holder_credentials(
    draw: ReverseDraw, *, reset_keys: set[str] | None = None
) -> list[dict]:
    """Create missing credentials, prune unused ones, and return new codes once."""
    active = _active_holder_names(draw)
    reset_keys = reset_keys or set()
    draw.holder_credentials = {
        key: value
        for key, value in draw.holder_credentials.items()
        if key in active
    }
    generated = []
    for key, display_name in sorted(active.items()):
        credential = draw.holder_credentials.get(key)
        if credential is not None and key not in reset_keys:
            credential["name"] = display_name
            code = _derive_holder_code(key, credential)
            credential["digest"] = _holder_code_digest(key, code)
            credential["code_scheme"] = "derived-v1"
            continue
        credential = {
            "id": secrets.token_urlsafe(12),
            "name": display_name,
            "created_at": now_iso(),
            "code_scheme": "derived-v1",
        }
        code = _derive_holder_code(key, credential)
        credential["digest"] = _holder_code_digest(key, code)
        draw.holder_credentials[key] = credential
        generated.append({"name": display_name, "code": code})
    return generated


def _trader_token(credential: dict, seconds: int) -> str:
    credential_id = str(
        credential.get("credential_external_id") or credential["id"]
    )
    digest = str(credential.get("code_digest") or credential["digest"])
    exp = int(time.time() + seconds)
    signature = _sign(f"trader.{credential_id}.{exp}.{digest}")
    return f"{credential_id}.{exp}.{signature}"


def _trader_identity(request: Request, draw: ReverseDraw) -> tuple[str, dict] | None:
    token = request.cookies.get(TRADER_COOKIE, "")
    parts = token.split(".")
    if len(parts) != 3:
        return None
    credential_id, exp, signature = parts
    if not exp.isdigit() or int(exp) <= time.time():
        return None
    match = next(
        (
            (key, credential)
            for key, credential in draw.holder_credentials.items()
            if hmac.compare_digest(
                str(credential.get("id", "")), credential_id
            )
        ),
        None,
    )
    if match is None or match[0] not in _active_holder_names(draw):
        return None
    expected = _sign(
        f"trader.{credential_id}.{exp}.{match[1].get('digest', '')}"
    )
    if not hmac.compare_digest(signature, expected):
        return None
    return match


def _trader_payload(draw: ReverseDraw, holder_key: str) -> dict:
    name = _active_holder_names(draw)[holder_key]
    tickets = sorted(
        ticket
        for ticket, holder in draw.owners.items()
        if _person_key(holder) == holder_key
    )
    return {
        "authenticated": True,
        "name": name,
        "tickets": [
            {
                "ticket": ticket,
                "status": draw.status(ticket),
                # `active` is the machine-readable form of `status`, so the
                # trading dashboard can tag a stub IN/OUT without parsing prose.
                "active": ticket not in draw.eliminated_in,
            }
            for ticket in tickets
        ],
        # Draw context for the trading dashboard. The public /api/state is
        # gated behind the viewer access code, which a signed-in holder does
        # not necessarily have, so the counts it needs travel with the session.
        "draw": {
            "total": draw.total,
            "still_in": len(draw.active()),
            "rounds_done": draw.rounds_done,
            "rounds_total": len(draw.survivors),
            "finished": draw.finished,
        },
    }


_failures: dict[str, list[float]] = {}


def throttle(key: str, limit: int) -> None:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < 600]
    _failures[key] = recent
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 10 minutes.")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---- public access code -----------------------------------------------------
class CodeIn(BaseModel):
    code: str


@app.get("/api/access")
def access_status(request: Request):
    return {"required": bool(PUBLIC_CODE), "ok": is_viewer(request), "admin": is_admin(request)}


@app.post("/api/access")
def access_login(body: CodeIn, request: Request, response: Response):
    if not PUBLIC_CODE:
        return {"ok": True}
    key = f"public:{client_ip(request)}"
    throttle(key, 10)
    code = re.sub(r"\D", "", body.code)
    if not hmac.compare_digest(code.encode(), PUBLIC_CODE.encode()):
        _failures.setdefault(key, []).append(time.time())
        raise HTTPException(status_code=401, detail="That code isn't right. Try again.")
    _failures.pop(key, None)
    seconds = int(config.PUBLIC_SESSION_DAYS * 86400)
    set_cookie(response, PUBLIC_COOKIE, make_token("public", seconds, _code_fingerprint()), seconds)
    return {"ok": True}


@app.post("/api/access/logout")
def access_logout(response: Response):
    response.delete_cookie(PUBLIC_COOKIE)
    return {"ok": True}


# ---- holder trading login --------------------------------------------------
class TraderLoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=20)


@app.get("/api/trading/session")
def trading_session(request: Request):
    draw = load()
    identity = _trader_identity(request, draw)
    if identity is None:
        return {"authenticated": False}
    return _trader_payload(draw, identity[0])


@app.post("/api/trading/login")
def trading_login(body: TraderLoginIn, request: Request, response: Response):
    holder_key = _person_key(body.name)
    code = body.code.strip()
    ip_failure_key = f"trader-ip:{client_ip(request)}"
    holder_failure_key = (
        f"trader-holder:{client_ip(request)}:"
        f"{hashlib.sha256(holder_key.encode()).hexdigest()[:16]}"
    )
    throttle(ip_failure_key, 20)
    throttle(holder_failure_key, 5)
    draw = load()
    credential = draw.holder_credentials.get(holder_key)
    supplied_digest = _holder_code_digest(holder_key, code)
    expected_digest = str((credential or {}).get("digest", "0" * 64))
    valid = (
        bool(re.fullmatch(r"\d{6}", code))
        and holder_key in _active_holder_names(draw)
        and credential is not None
        and hmac.compare_digest(supplied_digest, expected_digest)
    )
    if not valid:
        now = time.time()
        _failures.setdefault(ip_failure_key, []).append(now)
        _failures.setdefault(holder_failure_key, []).append(now)
        raise HTTPException(
            status_code=401,
            detail="The name or six-digit access code is incorrect.",
        )
    _failures.pop(holder_failure_key, None)
    seconds = int(config.TRADER_SESSION_DAYS * 86400)
    response.set_cookie(
        TRADER_COOKIE,
        _trader_token(credential, seconds),
        max_age=seconds,
        httponly=True,
        secure=ON_RENDER,
        samesite="strict",
        path="/",
    )
    return _trader_payload(draw, holder_key)


@app.post("/api/trading/logout")
def trading_logout(response: Response):
    response.delete_cookie(
        TRADER_COOKIE,
        path="/",
        secure=ON_RENDER,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


# ---- admin password -----------------------------------------------------------
class LoginIn(BaseModel):
    password: str


@app.post("/api/admin/login")
def login(body: LoginIn, request: Request, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured on the server.")
    key = f"admin:{client_ip(request)}"
    throttle(key, 8)
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        _failures.setdefault(key, []).append(time.time())
        raise HTTPException(status_code=401, detail="Wrong password.")
    _failures.pop(key, None)
    seconds = int(config.ADMIN_SESSION_HOURS * 3600)
    set_cookie(response, ADMIN_COOKIE, make_token("admin", seconds), seconds)
    return {"ok": True}


@app.post("/api/admin/logout")
def logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE)
    return {"ok": True}


# =============================================================================
# Pages
# =============================================================================
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html", headers=NO_CACHE)


@app.get("/trading", include_in_schema=False)
def trading_page():
    return FileResponse(STATIC / "trading.html", headers=NO_CACHE)


@app.get("/trading/login", include_in_schema=False)
def trading_login_page():
    return FileResponse(STATIC / "trading-login.html", headers=NO_CACHE)


@app.get("/trading/dashboard", include_in_schema=False)
def trading_dashboard_page():
    return FileResponse(STATIC / "trading-dashboard.html", headers=NO_CACHE)


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return FileResponse(
        STATIC / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers=NO_CACHE,
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# =============================================================================
# Public API
# =============================================================================
@app.get("/api/config")
def get_config():
    return {
        "org_name": config.ORG_NAME,
        "org_initials": config.ORG_INITIALS
        or "".join(w[0] for w in config.ORG_NAME.split()[:2]).upper(),
        "posted_in": config.POSTED_IN,
        "prize_text": config.PRIZE_TEXT,
        "closing_note": config.CLOSING_NOTE,
        # The landing screen is shown before the access code is accepted, so it
        # reads its headline figures from here rather than from /api/state.
        "schedule": current_schedule(),
        "grid_columns": config.GRID_COLUMNS,
        "highlight_last_round": config.HIGHLIGHT_LAST_ROUND,
        "show_holder_names": config.PUBLIC_SHOW_HOLDER_NAMES,
        "refresh_seconds": config.PUBLIC_REFRESH_SECONDS,
        "colors": {
            "active": config.COLOR_ACTIVE,
            "eliminated": config.COLOR_ELIMINATED,
            "last_round": config.COLOR_LAST_ROUND,
            "winner": config.COLOR_WINNER,
            "prize": config.COLOR_PRIZE,
            "highlight": config.COLOR_HIGHLIGHT,
        },
    }


@app.get("/api/state", dependencies=[Depends(require_viewer)])
def get_state():
    with _cache_lock:
        hit = _public_cache.get("state")
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        payload = public_payload(load())
        _public_cache["state"] = (time.time(), payload)
        return payload


# =============================================================================
# Admin API
# =============================================================================
admin = [Depends(require_admin)]


def require_writable() -> None:
    if config.MAINTENANCE_MODE:
        raise HTTPException(
            status_code=503,
            detail=(
                "The application is temporarily read-only for maintenance."
            ),
        )


admin_write = [Depends(require_admin), Depends(require_writable)]


@app.get("/api/admin/state", dependencies=admin)
def admin_state():
    return admin_payload(load())


class ExpectRound(BaseModel):
    # The round number the admin is looking at; stops a double-click or a second
    # admin tab from running two rounds.
    expected_rounds_done: int


@app.post("/api/admin/rounds/next", dependencies=admin_write)
def run_next(body: ExpectRound):
    try:
        with Mutation() as d:
            if d.rounds_done != body.expected_rounds_done:
                raise DrawError("The draw changed since your page loaded. Refresh and try again.")
            d.run_next_round()
            log.info("Ran %s", d.rounds[-1]["label"])
            return admin_payload(d)
    except DrawError as e:
        bad_request(e)


@app.post("/api/admin/rounds/undo", dependencies=admin_write)
def undo(body: ExpectRound):
    try:
        with Mutation() as d:
            if d.rounds_done != body.expected_rounds_done:
                raise DrawError("The draw changed since your page loaded. Refresh and try again.")
            rec = d.undo_last_round()
            log.warning("Undid %s", rec["label"])
            return admin_payload(d)
    except DrawError as e:
        bad_request(e)


class ResetIn(BaseModel):
    keep_holders: bool = True
    confirm: str = Field(description='Must be "RESET"')


@app.post("/api/admin/reset", dependencies=admin_write)
def reset(body: ResetIn):
    if body.confirm != "RESET":
        bad_request(DrawError("Type RESET to confirm."))
    with Mutation() as d:
        d.reset(keep_owners=body.keep_holders)
        log.warning("Draw reset (keep_holders=%s)", body.keep_holders)
        return admin_payload(d)


class HoldersIn(BaseModel):
    csv: str
    mode: str = "replace"  # "replace" or "merge"


@app.put("/api/admin/holders", dependencies=admin_write)
def put_holders(body: HoldersIn):
    try:
        with Mutation() as d:
            mapping = d.parse_owner_csv(body.csv)
            d.set_owners(mapping if body.mode == "replace" else {**d.owners, **mapping})
            new_credentials = _sync_holder_credentials(d)
            d.allocation_source_fingerprint = _source_fingerprint(
                d.source_dataframe
            )
            out = admin_payload(d)
            out["imported"] = len(mapping)
            out["new_trading_credentials"] = new_credentials
            return out
    except DrawError as e:
        bad_request(e)


def _read_upload_dataframe(data: bytes, filename: str) -> pd.DataFrame:
    """Read CSV or Excel upload bytes into a pandas DataFrame."""
    suffix = Path(filename).suffix.lower()
    source = io.BytesIO(data)
    if suffix == ".csv":
        frame = pd.read_csv(source, dtype=object)
    elif suffix in {".xlsx", ".xlsm", ".xls"}:
        workbook = pd.ExcelFile(source)
        frame = pd.DataFrame()
        for sheet_name in workbook.sheet_names:
            candidate = workbook.parse(sheet_name=sheet_name, dtype=object)
            if not candidate.dropna(how="all").empty:
                frame = candidate
                break
    else:
        raise DrawError(
            "Choose a CSV or Excel file (.csv, .xlsx, .xlsm, or .xls)."
        )

    frame = frame.dropna(how="all").reset_index(drop=True)
    if frame.empty:
        raise DrawError("The selected file has no data rows.")
    if len(frame) > 200_000:
        raise DrawError("The file has too many rows (maximum 200,000).")
    return frame


def _column_key(value: object) -> str:
    """Normalize verbose form-export headers for matching."""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip().lower()


def _assignment_dataframe(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Normalize conventional ticket/name columns when they exist."""
    keyed = {_column_key(column): column for column in frame.columns}
    ticket_aliases = (
        "ticket",
        "ticket #",
        "ticket no",
        "ticket number",
        "number",
    )
    holder_aliases = ("name", "holder", "ticket holder", "owner")
    ticket_column = next(
        (keyed[name] for name in ticket_aliases if name in keyed), None
    )
    holder_column = next(
        (keyed[name] for name in holder_aliases if name in keyed), None
    )
    if ticket_column is None or holder_column is None:
        return None

    return pd.DataFrame(
        {"ticket": frame[ticket_column], "name": frame[holder_column]}
    )


NAME_HEADERS = (
    "full name",
    "name",
    "purchaser",
    "purchaser name",
    "buyer",
    "buyer name",
    "holder",
    "ticket holder",
    "employee",
    "employee name",
    "attendee",
    "attendee name",
)
QUANTITY_HEADERS = (
    "quantity",
    "qty",
    "tickets",
    "ticket quantity",
    "number of tickets",
    "no of tickets",
    "# of tickets",
    "how many tickets",
)


def _pick_column(columns: list[tuple[str, object]], headers: tuple[str, ...], *,
                 contains: tuple[str, ...] = ()) -> object | None:
    """First column matching one of `headers` exactly, else one containing a phrase."""
    for header in headers:
        for key, column in columns:
            if key == header:
                return column
    for phrase in contains:
        for key, column in columns:
            if phrase in key:
                return column
    return None


def _choice_numbers(value: object) -> list[int]:
    """Ticket numbers written in one 'preferred ticket number' answer."""
    return [int(n) for n in re.findall(r"\d+", str(value or ""))]


def _orders_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per order: who bought, how many, and which numbers they asked for."""
    columns = [(_column_key(column), column) for column in frame.columns]
    name_column = _pick_column(columns, NAME_HEADERS, contains=("name",))
    quantity_column = _pick_column(
        columns, QUANTITY_HEADERS, contains=("how many", "quantity")
    )
    preference_columns = [
        column
        for key, column in columns
        if column != quantity_column
        and ("choice" in key and "ticket" in key or "preferred ticket" in key)
    ]
    if name_column is None or quantity_column is None:
        raise DrawError(
            "No ticket/name columns were found, and no order columns either. "
            "The file needs a name column and a ticket quantity column."
        )

    orders = pd.DataFrame(
        {
            "name": frame[name_column].fillna("").astype(str).str.strip(),
            "quantity": pd.to_numeric(
                frame[quantity_column].astype(str).str.extract(r"(\d+)")[0],
                errors="coerce",
            )
            .fillna(0)
            .astype(int),
        }
    )
    # Per order, the numbers that order asked for, in preference order.
    orders["choices"] = [
        [n for column in preference_columns for n in _choice_numbers(row[column])]
        for _, row in frame.iterrows()
    ]
    return orders[(orders["name"] != "") & (orders["quantity"] > 0)]


def _holders_by_name(orders: pd.DataFrame) -> pd.DataFrame:
    """Combine each person's orders into one row, sorted by holder name."""
    orders = orders.assign(key=orders["name"].str.casefold())
    grouped = orders.groupby("key", sort=False)
    people = pd.DataFrame(
        {"name": grouped["name"].first(), "quantity": grouped["quantity"].sum()}
    )
    # Kept as a list per order, because one order's numbers are alternatives.
    people["choices"] = grouped["choices"].apply(list)
    return people.sort_values(
        "name", key=lambda names: names.str.casefold()
    ).reset_index(drop=True)


def _order_dataframe(frame: pd.DataFrame, total: int) -> pd.DataFrame:
    """Expand an order export into ticket holders, allocated in holder order."""
    people = _holders_by_name(_orders_dataframe(frame))
    requested = int(people["quantity"].sum())
    if requested > total:
        raise DrawError(
            f"The file requests {requested:,} tickets, but the draw has only "
            f"{total:,}."
        )

    assignments: dict[int, str] = {}
    reserved: dict[int, int] = {}

    # The numbers offered on one order row are alternatives, so each row wins at
    # most one of them. Holders are served alphabetically, so the same file
    # always allocates the same way.
    for index, person in people.iterrows():
        taken = 0
        for order_choices in person["choices"]:
            if taken >= person["quantity"]:
                break
            for preferred in order_choices:
                if 1 <= preferred <= total and preferred not in assignments:
                    assignments[preferred] = person["name"]
                    taken += 1
                    break
        reserved[index] = taken

    # Everyone else is filled from the lowest free numbers, still in holder
    # order, so each person's tickets land together on the board.
    available = (ticket for ticket in range(1, total + 1) if ticket not in assignments)
    for index, person in people.iterrows():
        for _ in range(int(person["quantity"]) - reserved[index]):
            assignments[next(available)] = person["name"]

    return pd.DataFrame(sorted(assignments.items()), columns=["ticket", "name"])


def _uploaded_holders_dataframe(
    data: bytes, filename: str, total: int
) -> pd.DataFrame:
    """Return a validated ticket/name DataFrame for any supported upload."""
    source = _read_upload_dataframe(data, filename)
    return _holders_from_dataframe(source, total)


def _holders_from_dataframe(
    source: pd.DataFrame, total: int
) -> pd.DataFrame:
    """Build ticket assignments without modifying the source DataFrame."""
    holders = _assignment_dataframe(source)
    if holders is None:
        holders = _order_dataframe(source, total)

    holders["ticket"] = pd.to_numeric(holders["ticket"], errors="coerce")
    holders["name"] = holders["name"].fillna("").astype(str).str.strip()
    holders = holders.dropna(subset=["ticket"])
    holders = holders[holders["ticket"].mod(1).eq(0)]
    holders["ticket"] = holders["ticket"].astype(int)
    holders = holders[
        holders["ticket"].between(1, total) & holders["name"].ne("")
    ]
    return holders.drop_duplicates("ticket", keep="last").sort_values("ticket")


def _dataframe_payload(frame: pd.DataFrame, metadata: dict) -> dict:
    """Return the complete DataFrame as JSON-safe browser table data."""
    split = json.loads(frame.to_json(orient="split", date_format="iso"))
    return {
        "filename": metadata.get("filename", "Uploaded data"),
        "uploaded_at": metadata.get("uploaded_at", ""),
        "columns": [str(column) for column in split["columns"]],
        "rows": split["data"],
        "row_count": len(split["data"]),
        "column_count": len(split["columns"]),
    }


def _stored_dataframe(record: dict | None) -> tuple[pd.DataFrame, dict] | None:
    """Reconstruct the persisted DataFrame and its metadata."""
    if not record or not record.get("json"):
        return None
    frame = pd.read_json(io.StringIO(record["json"]), orient="split")
    return frame, record


def _source_fingerprint(record: dict | None) -> str:
    """Stable identity for the complete uploaded source sheet."""
    if not record or not record.get("json"):
        return ""
    return str(
        record.get("fingerprint")
        or hashlib.sha256(str(record["json"]).encode()).hexdigest()
    )


def _allocation_fingerprint(owners: dict[int, str]) -> str:
    canonical = json.dumps(
        [[ticket, owners[ticket]] for ticket in sorted(owners)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _person_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _email_key(value: object) -> str:
    return str(value or "").strip().casefold()


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_HEADERS = (
    "hoopp email address",
    "email address",
    "email",
    "e-mail address",
    "e-mail",
)


def _source_people(frame: pd.DataFrame) -> dict[str, dict]:
    """Resolve normalized source names to their distinct source emails."""
    columns = [(_column_key(column), column) for column in frame.columns]
    name_column = _pick_column(columns, NAME_HEADERS, contains=("name",))
    email_column = _pick_column(columns, EMAIL_HEADERS, contains=("email",))
    if name_column is None or email_column is None:
        return {}

    people: dict[str, dict] = {}
    for _, row in frame.iterrows():
        name = str(row[name_column] or "").strip()
        key = _person_key(name)
        if not key:
            continue
        person = people.setdefault(key, {"name": name, "emails": set()})
        email = _email_key(row[email_column])
        if email and EMAIL_PATTERN.fullmatch(email):
            person["emails"].add(email)
    return people


def _successful_delivery_pairs(draw: ReverseDraw) -> set[tuple[str, int]]:
    return {
        (_email_key(job.get("email")), int(ticket))
        for batch in draw.notification_batches
        for job in batch.get("jobs", [])
        if job.get("status") == "sent"
        for ticket in job.get("new_tickets", [])
    }


def _unknown_delivery_pairs(draw: ReverseDraw) -> set[tuple[str, int]]:
    return {
        (_email_key(job.get("email")), int(ticket))
        for batch in draw.notification_batches
        for job in batch.get("jobs", [])
        if job.get("status") == "unknown"
        for ticket in job.get("new_tickets", [])
    }


def _batch_is_active(batch: dict) -> bool:
    if batch.get("status") != "sending":
        return False
    try:
        created_at = datetime.fromisoformat(str(batch["created_at"]))
        age = datetime.now(timezone.utc) - created_at
        return age.total_seconds() < 30 * 60
    except (KeyError, TypeError, ValueError):
        return False


def _expire_stale_batches(draw: ReverseDraw) -> None:
    for batch in draw.notification_batches:
        if batch.get("status") != "sending" or _batch_is_active(batch):
            continue
        for job in batch.get("jobs", []):
            if job.get("status") == "pending":
                job.update(
                    status="failed",
                    error="The email worker stopped before this message was sent.",
                )
        batch["status"] = "attention"
        batch["completed_at"] = now_iso()


def _notification_preview(draw: ReverseDraw) -> dict:
    """Pure current-state view of recipients with tickets not yet delivered."""
    settings = email_config()
    current_source = _source_fingerprint(draw.source_dataframe)
    source_matches = bool(current_source) and (
        current_source == draw.allocation_source_fingerprint
    )
    result = {
        "configured": settings.configured,
        "provider": settings.provider,
        "sender": settings.sender,
        "missing_settings": settings.missing,
        "source_matches_allocation": source_matches,
        "allocation_fingerprint": _allocation_fingerprint(draw.owners),
        "recipients": [],
        "blocked": [],
        "pending_people": 0,
        "pending_tickets": 0,
        "history": list(reversed(draw.notification_batches[-10:])),
    }
    if not draw.owners:
        result["ready"] = False
        result["reason"] = "Save ticket holders before sending email."
        return result
    if not source_matches:
        result["ready"] = False
        result["reason"] = (
            "The saved ticket allocation does not match the latest uploaded "
            "source sheet. Review it, then Save or Merge the holders."
        )
        return result

    stored = _stored_dataframe(draw.source_dataframe)
    if stored is None:
        result["ready"] = False
        result["reason"] = "Upload a source spreadsheet containing email addresses."
        return result
    source_people = _source_people(stored[0])
    tickets_by_person: dict[str, dict] = {}
    for ticket, holder in sorted(draw.owners.items()):
        key = _person_key(holder)
        person = tickets_by_person.setdefault(
            key, {"name": holder, "tickets": []}
        )
        person["tickets"].append(ticket)

    deliveries = _successful_delivery_pairs(draw)
    unknown_deliveries = _unknown_delivery_pairs(draw)
    recipients_by_email: dict[str, dict] = {}
    for key, allocation in tickets_by_person.items():
        source_person = source_people.get(key)
        emails = source_person["emails"] if source_person else set()
        if len(emails) != 1:
            result["blocked"].append(
                {
                    "name": allocation["name"],
                    "tickets": allocation["tickets"],
                    "reason": (
                        "No valid email found in the source sheet."
                        if not emails
                        else "Conflicting email addresses found in the source sheet."
                    ),
                }
            )
            continue
        email = next(iter(emails))
        recipient = recipients_by_email.setdefault(
            email,
            {"email": email, "name": allocation["name"], "all_tickets": []},
        )
        recipient["all_tickets"].extend(allocation["tickets"])

    for recipient in recipients_by_email.values():
        recipient["all_tickets"] = sorted(set(recipient["all_tickets"]))
        uncertain = [
            ticket
            for ticket in recipient["all_tickets"]
            if (recipient["email"], ticket) in unknown_deliveries
        ]
        if uncertain:
            result["blocked"].append(
                {
                    "name": recipient["name"],
                    "tickets": uncertain,
                    "reason": (
                        "The email provider returned an uncertain result for these "
                        "tickets. Verify delivery before trying again."
                    ),
                }
            )
            continue
        recipient["new_tickets"] = [
            ticket
            for ticket in recipient["all_tickets"]
            if (recipient["email"], ticket) not in deliveries
        ]
        if recipient["new_tickets"]:
            result["recipients"].append(recipient)

    result["recipients"].sort(key=lambda item: item["name"].casefold())
    result["pending_people"] = len(result["recipients"])
    result["pending_tickets"] = sum(
        len(item["new_tickets"]) for item in result["recipients"]
    )
    result["ready"] = bool(
        settings.configured
        and result["recipients"]
        and not result["blocked"]
    )
    active_batch = next(
        (
            batch
            for batch in reversed(draw.notification_batches)
            if _batch_is_active(batch)
        ),
        None,
    )
    if active_batch:
        result["ready"] = False
        result["reason"] = "An email batch is currently sending."
    elif result["blocked"]:
        result["reason"] = "Fix the blocked recipients before sending."
    elif not settings.configured:
        result["reason"] = (
            "Email delivery is not configured. Contact the site owner."
        )
    elif not result["recipients"]:
        result["reason"] = "Everyone's current tickets have already been emailed."
    else:
        result["reason"] = ""
    result["sending"] = bool(active_batch)
    return result


@app.get("/api/admin/holders/dataframe", dependencies=admin)
def get_holder_dataframe():
    stored = _stored_dataframe(load().source_dataframe)
    if stored is None:
        return {"dataframe": None}
    frame, metadata = stored
    return {"dataframe": _dataframe_payload(frame, metadata)}


@app.post("/api/admin/holders/file", dependencies=admin_write)
async def preview_holder_file(request: Request, filename: str):
    content_length = request.headers.get("content-length")
    too_large = (
        content_length
        and content_length.isdigit()
        and int(content_length) > 10 * 1024 * 1024
    )
    if too_large:
        raise HTTPException(
            status_code=413, detail="Files must be 10 MB or smaller."
        )
    data = await request.body()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail="Files must be 10 MB or smaller."
        )
    if not data:
        bad_request(DrawError("The selected file is empty."))

    try:
        source = _read_upload_dataframe(data, filename)
        with Mutation() as draw:
            holders = _holders_from_dataframe(source, draw.total)
            draw.source_dataframe = {
                "filename": Path(filename).name,
                "uploaded_at": now_iso(),
                "json": source.to_json(orient="split", date_format="iso"),
            }
            draw.source_dataframe["fingerprint"] = _source_fingerprint(
                draw.source_dataframe
            )
            dataframe = _dataframe_payload(source, draw.source_dataframe)
        # Per-person preview so the admin can check the allocation before saving.
        people = holders.groupby("name", sort=True)["ticket"].agg(list)
        return {
            "csv": holders.to_csv(index=False),
            "imported": len(holders),
            "dataframe": dataframe,
            "people": [
                {"name": name, "tickets": tickets} for name, tickets in people.items()
            ],
        }
    except (DrawError, ValueError, KeyError, OSError) as error:
        bad_request(DrawError(f"Could not read {filename}: {error}"))
    except Exception:
        log.exception("Failed to parse holder workbook %s", filename)
        bad_request(
            DrawError(
                f"Could not read {filename}. Make sure it is a valid, "
                "unprotected Excel file."
            )
        )


@app.get("/api/admin/notifications/preview", dependencies=admin)
def preview_notifications():
    return _notification_preview(load())


def _process_notification_batch(batch_id: str, recipients: list[dict]) -> None:
    """Deliver one claimed batch and durably record every result."""
    client = build_email_client(email_config())
    for recipient in recipients:
        current = next(
            (
                item
                for item in load().notification_batches
                if item.get("id") == batch_id
            ),
            None,
        )
        if current is None or current.get("status") != "sending":
            return
        job_status = "sent"
        sent_at = ""
        error_message = ""
        request_id = ""
        try:
            current_draw = load()
            holder_key = _person_key(recipient["name"])
            credential = current_draw.holder_credentials.get(holder_key)
            if credential is None:
                with Mutation() as credential_draw:
                    _sync_holder_credentials(credential_draw)
                current_draw = load()
                credential = current_draw.holder_credentials.get(holder_key)
            if credential is None:
                raise EmailSendError("Could not create a trading login code.")
            response = client.send_ticket_email(
                recipient=recipient["email"],
                name=recipient["name"],
                new_tickets=recipient["new_tickets"],
                all_tickets=recipient["all_tickets"],
                trading_code=_derive_holder_code(holder_key, credential),
            )
            sent_at = now_iso()
            request_id = response.get("request_id", "")
        except EmailSendError as error:
            job_status = "unknown" if error.outcome_unknown else "failed"
            error_message = str(error)[:1000]
            log.error("Ticket email to %s failed: %s", recipient["email"], error)
        except Exception as error:
            job_status = "failed"
            error_message = f"Unexpected email provider error: {error}"[:1000]
            log.exception("Unexpected ticket email failure for %s", recipient["email"])

        with Mutation() as draw:
            batch = next(
                item for item in draw.notification_batches if item["id"] == batch_id
            )
            job = next(
                item for item in batch["jobs"]
                if item["email"] == recipient["email"]
            )
            job.update(
                status=job_status,
                sent_at=sent_at,
                error=error_message,
                provider_request_id=request_id,
            )

    with Mutation() as draw:
        batch = next(
            item for item in draw.notification_batches if item["id"] == batch_id
        )
        statuses = {job["status"] for job in batch["jobs"]}
        batch["status"] = "completed" if statuses == {"sent"} else "attention"
        batch["completed_at"] = now_iso()


@app.post("/api/admin/notifications/send-all", dependencies=admin_write)
def send_all_notifications(background_tasks: BackgroundTasks):
    batch_id = uuid.uuid4().hex
    with Mutation() as draw:
        _expire_stale_batches(draw)
        _sync_holder_credentials(draw)
        if any(
            _batch_is_active(batch)
            for batch in draw.notification_batches
        ):
            raise HTTPException(
                status_code=409,
                detail="Another email batch is already sending.",
            )
        preview = _notification_preview(draw)
        if not preview["ready"]:
            raise HTTPException(status_code=409, detail=preview["reason"])
        batch = {
            "id": batch_id,
            "created_at": now_iso(),
            "completed_at": "",
            "status": "sending",
            "allocation_fingerprint": preview["allocation_fingerprint"],
            "jobs": [
                {
                    **recipient,
                    "status": "pending",
                    "sent_at": "",
                    "error": "",
                    "provider_request_id": "",
                }
                for recipient in preview["recipients"]
            ],
        }
        draw.notification_batches.append(batch)
    background_tasks.add_task(
        _process_notification_batch, batch_id, preview["recipients"]
    )
    return {"batch": batch, "preview": _notification_preview(load())}


@app.post("/api/admin/notifications/cancel", dependencies=admin_write)
def cancel_notification_batch():
    with Mutation() as draw:
        batch = next(
            (
                item
                for item in reversed(draw.notification_batches)
                if _batch_is_active(item)
            ),
            None,
        )
        if batch is None:
            raise HTTPException(status_code=409, detail="No email batch is sending.")
        for job in batch.get("jobs", []):
            if job.get("status") == "pending":
                job.update(
                    status="failed",
                    error="Canceled by the administrator before this message sent.",
                )
        batch["status"] = "attention"
        batch["completed_at"] = now_iso()
        return _notification_preview(draw)


@app.post("/api/admin/notifications/clear-history", dependencies=admin_write)
def clear_notification_history():
    with Mutation() as draw:
        _expire_stale_batches(draw)
        if any(_batch_is_active(batch) for batch in draw.notification_batches):
            raise HTTPException(
                status_code=409,
                detail="Cancel the sending email batch before clearing history.",
            )
        cleared_batches = len(draw.notification_batches)
        draw.notification_batches.clear()
        return {
            "cleared_batches": cleared_batches,
            "preview": _notification_preview(draw),
        }


class ResolveUnknownIn(BaseModel):
    batch_id: str
    email: str
    delivered: bool


@app.post("/api/admin/notifications/resolve-unknown", dependencies=admin_write)
def resolve_unknown_notification(body: ResolveUnknownIn):
    with Mutation() as draw:
        batch = next(
            (
                item
                for item in draw.notification_batches
                if item.get("id") == body.batch_id
            ),
            None,
        )
        if batch is None:
            raise HTTPException(status_code=404, detail="Email batch not found.")
        job = next(
            (
                item
                for item in batch.get("jobs", [])
                if _email_key(item.get("email")) == _email_key(body.email)
            ),
            None,
        )
        if job is None or job.get("status") != "unknown":
            raise HTTPException(
                status_code=409,
                detail="That email no longer has an unknown delivery result.",
            )
        if body.delivered:
            job.update(
                status="sent",
                sent_at=now_iso(),
                error="Manually marked delivered after checking the sender mailbox.",
            )
        else:
            job.update(
                status="failed",
                error="Manually cleared for retry after checking the sender mailbox.",
            )
        statuses = {item["status"] for item in batch["jobs"]}
        batch["status"] = "completed" if statuses == {"sent"} else "attention"
        return _notification_preview(draw)


class BlockIn(BaseModel):
    name: str
    start: int
    end: int


class UnassignIn(BaseModel):
    start: int | None = None
    end: int | None = None
    name: str = ""


@app.post("/api/admin/holders/block", dependencies=admin_write)
def assign_block(body: BlockIn):
    try:
        with Mutation() as d:
            n = d.assign_block(body.name, body.start, body.end)
            new_credentials = _sync_holder_credentials(d)
            out = admin_payload(d)
            out["assigned"] = n
            out["new_trading_credentials"] = new_credentials
            return out
    except DrawError as e:
        bad_request(e)


@app.post("/api/admin/holders/unassign", dependencies=admin_write)
def unassign_holders(body: UnassignIn):
    try:
        with Mutation() as draw:
            if body.name.strip():
                removed = draw.unassign_holder(body.name)
            elif body.start is not None:
                removed = draw.unassign_block(
                    body.start, body.end if body.end is not None else body.start
                )
            else:
                raise DrawError("Enter a holder name or ticket range.")
            _sync_holder_credentials(draw)
            out = admin_payload(draw)
            out["unassigned"] = removed
            return out
    except DrawError as error:
        bad_request(error)


class HolderCredentialIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@app.post(
    "/api/admin/trading/credentials/generate", dependencies=admin_write
)
def generate_missing_holder_credentials():
    with Mutation() as draw:
        generated = _sync_holder_credentials(draw)
        out = admin_payload(draw)
        out["new_trading_credentials"] = generated
        return out


@app.post(
    "/api/admin/trading/credentials/reset", dependencies=admin_write
)
def reset_holder_credential(body: HolderCredentialIn):
    holder_key = _person_key(body.name)
    with Mutation() as draw:
        if holder_key not in _active_holder_names(draw):
            raise HTTPException(status_code=404, detail="Ticket holder not found.")
        generated = _sync_holder_credentials(draw, reset_keys={holder_key})
        out = admin_payload(draw)
        out["new_trading_credentials"] = generated
        return out


def csv_response(text: str, name: str) -> PlainTextResponse:
    return PlainTextResponse(
        text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@app.get("/api/admin/export/log.csv", dependencies=admin)
def export_log():
    return csv_response(load().log_csv(), "elimination_log.csv")


@app.get("/api/admin/export/tickets.csv", dependencies=admin)
def export_tickets():
    return csv_response(load().tickets_csv(), "ticket_status.csv")


@app.get("/api/admin/export/holders.csv", dependencies=admin)
def export_holders():
    return csv_response(load().owners_csv(), "ticket_holders.csv")


def _relational_database():
    if relational_database is None:
        raise RuntimeError("Relational storage has not started.")
    return relational_database


def _new_relational_credential(
    holder_key: str, display_name: str
):
    from .services.holder_service import CredentialProvision

    external_id = secrets.token_urlsafe(12)
    credential = {"credential_external_id": external_id}
    code = _derive_holder_code(holder_key, credential)
    return CredentialProvision(
        external_id=external_id,
        digest=_holder_code_digest(holder_key, code),
        readable_code=code,
    )


def _set_trader_cookie(response: Response, token: str, seconds: int) -> None:
    response.set_cookie(
        TRADER_COOKIE,
        token,
        max_age=seconds,
        httponly=True,
        secure=ON_RENDER,
        samesite="strict",
        path="/",
    )


def _clear_trader_cookie(response: Response) -> None:
    response.delete_cookie(
        TRADER_COOKIE,
        path="/",
        secure=ON_RENDER,
        httponly=True,
        samesite="strict",
    )


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


def _clear_failure(key: str) -> None:
    _failures.pop(key, None)


if config.RELATIONAL_STORE_ENABLED:
    from .relational_api import (
        RelationalAPIContext,
        create_relational_router,
        install_relational_router,
    )

    install_relational_router(
        app,
        create_relational_router(
            RelationalAPIContext(
                database=_relational_database,
                require_admin=require_admin,
                require_viewer=require_viewer,
                credential_factory=_new_relational_credential,
                derive_holder_code=_derive_holder_code,
                holder_code_digest=_holder_code_digest,
                sign=_sign,
                trader_token=_trader_token,
                set_trader_cookie=_set_trader_cookie,
                clear_trader_cookie=_clear_trader_cookie,
                throttle=throttle,
                client_ip=client_ip,
                record_failure=_record_failure,
                clear_failure=_clear_failure,
                parse_upload=_read_upload_dataframe,
                holders_from_frame=_holders_from_dataframe,
                dataframe_payload=_dataframe_payload,
                source_people=_source_people,
            )
        ),
    )
