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
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:  # load a local .env file if present (not needed on Render)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from . import config
from .db import make_store
from .draw import DrawError, ReverseDraw, current_schedule

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reverse_draw")

STATIC = Path(__file__).parent / "static"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
PUBLIC_CODE = (os.getenv("PUBLIC_ACCESS_CODE") or str(config.PUBLIC_ACCESS_CODE or "")).strip()
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
ON_RENDER = bool(os.getenv("RENDER"))
ADMIN_COOKIE = "rd_admin"
PUBLIC_COOKIE = "rd_public"

store = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global store
    store = make_store()
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set - the admin page is disabled.")
    if PUBLIC_CODE and not re.fullmatch(r"\d{6}", PUBLIC_CODE):
        log.warning("PUBLIC_ACCESS_CODE should be exactly 6 digits (got %d characters).", len(PUBLIC_CODE))
    if not os.getenv("SECRET_KEY"):
        log.warning("SECRET_KEY is not set - logins will reset whenever the server restarts.")
    if ON_RENDER and store.kind == "sqlite":
        log.error("Running on Render without DATABASE_URL - the draw will be LOST on every restart.")
    yield
    store.close()


app = FastAPI(title="Reverse Draw", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
    out.update(
        holders={str(t): n for t, n in d.owners.items()},
        rounds=[{**round_summary(r), "seed": r["seed"], "eliminated": r["eliminated"]} for r in d.rounds],
        undone=[
            {**round_summary(r), "seed": r["seed"], "undone_at": r["undone_at"], "eliminated": r["eliminated"]}
            for r in d.undone
        ],
        summary=d.holder_summary(),
        storage=store.kind,
        warn_no_db=ON_RENDER and store.kind == "sqlite",
        schedule_pending=d.started and d.schedule != current_schedule(),
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


@app.get("/api/admin/state", dependencies=admin)
def admin_state():
    return admin_payload(load())


class ExpectRound(BaseModel):
    # The round number the admin is looking at; stops a double-click or a second
    # admin tab from running two rounds.
    expected_rounds_done: int


@app.post("/api/admin/rounds/next", dependencies=admin)
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


@app.post("/api/admin/rounds/undo", dependencies=admin)
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


@app.post("/api/admin/reset", dependencies=admin)
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


@app.put("/api/admin/holders", dependencies=admin)
def put_holders(body: HoldersIn):
    with Mutation() as d:
        mapping = d.parse_owner_csv(body.csv)
        d.set_owners(mapping if body.mode == "replace" else {**d.owners, **mapping})
        out = admin_payload(d)
        out["imported"] = len(mapping)
        return out


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


def _order_dataframe(frame: pd.DataFrame, total: int) -> pd.DataFrame:
    """Expand the supplied Microsoft Forms order export into ticket holders."""
    columns = [(_column_key(column), column) for column in frame.columns]
    name_column = next(
        (column for key, column in columns if key == "full name"), None
    )
    quantity_column = next(
        (
            column
            for key, column in columns
            if key.startswith("how many united way reverse draw")
        ),
        None,
    )
    preference_columns = [
        column
        for key, column in columns
        if "choice ticket number" in key
    ]
    if name_column is None or quantity_column is None:
        raise DrawError(
            "No ticket/name columns or Reverse Draw order columns were found."
        )

    orders = frame[[name_column, quantity_column, *preference_columns]].copy()
    orders["name"] = orders[name_column].fillna("").astype(str).str.strip()
    orders["quantity"] = pd.to_numeric(
        orders[quantity_column].astype(str).str.extract(r"(\d+)")[0],
        errors="coerce",
    ).fillna(0).astype(int)
    orders = orders[(orders["name"] != "") & (orders["quantity"] > 0)]
    requested = int(orders["quantity"].sum())
    if requested > total:
        raise DrawError(
            f"The file requests {requested:,} tickets, but the draw has only "
            f"{total:,}."
        )

    assignments: dict[int, str] = {}
    order_tickets: dict[object, list[int]] = {}

    # Ticket choices are alternatives. Reserve at most one valid preference
    # per order, in upload order, before filling the remaining ticket numbers.
    for index, order in orders.iterrows():
        allocated: list[int] = []
        for column in preference_columns:
            match = re.search(r"\d+", str(order[column]))
            preferred = int(match.group()) if match else 0
            if 1 <= preferred <= total and preferred not in assignments:
                assignments[preferred] = order["name"]
                allocated.append(preferred)
                break
        order_tickets[index] = allocated

    available = (
        ticket
        for ticket in range(1, total + 1)
        if ticket not in assignments
    )
    for index, order in orders.iterrows():
        allocated = order_tickets[index]
        for _ in range(int(order["quantity"]) - len(allocated)):
            ticket = next(available)
            assignments[ticket] = order["name"]
            allocated.append(ticket)

    return pd.DataFrame(
        sorted(assignments.items()), columns=["ticket", "name"]
    )


def _uploaded_holders_dataframe(
    data: bytes, filename: str, total: int
) -> pd.DataFrame:
    """Return a validated ticket/name DataFrame for any supported upload."""
    source = _read_upload_dataframe(data, filename)
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


@app.post("/api/admin/holders/file", dependencies=admin)
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
        draw = load()
        holders = _uploaded_holders_dataframe(data, filename, draw.total)
        return {
            "csv": holders.to_csv(index=False),
            "imported": len(holders),
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


class BlockIn(BaseModel):
    name: str
    start: int
    end: int


@app.post("/api/admin/holders/block", dependencies=admin)
def assign_block(body: BlockIn):
    try:
        with Mutation() as d:
            n = d.assign_block(body.name, body.start, body.end)
            out = admin_payload(d)
            out["assigned"] = n
            return out
    except DrawError as e:
        bad_request(e)


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
