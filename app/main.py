"""
Reverse Draw - FastAPI app.

Public:  GET /            board page
         GET /api/config  display settings
         GET /api/state   current draw (no holder names unless enabled in config)
Admin:   GET /admin       control page (password login)
         /api/admin/...   run/undo/reset rounds, manage holders, CSV exports
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
ON_RENDER = bool(os.getenv("RENDER"))
COOKIE = "rd_admin"

store = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global store
    store = make_store()
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set - the admin page is disabled.")
    if not os.getenv("SECRET_KEY"):
        log.warning("SECRET_KEY is not set - admin logins will reset whenever the server restarts.")
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
    )
    return out


def bad_request(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Admin auth (single shared password, signed HttpOnly cookie)
# =============================================================================
def _sign(msg: str) -> str:
    return hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = int(time.time() + config.ADMIN_SESSION_HOURS * 3600)
    return f"{exp}.{_sign(f'admin.{exp}')}"


def token_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(f"admin.{exp}"))


def require_admin(request: Request) -> None:
    if not token_ok(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="Not logged in.")


_failures: dict[str, list[float]] = {}


def throttle(ip: str) -> None:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < 600]
    _failures[ip] = recent
    if len(recent) >= 8:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 10 minutes.")


class LoginIn(BaseModel):
    password: str


@app.post("/api/admin/login")
def login(body: LoginIn, request: Request, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured on the server.")
    ip = request.client.host if request.client else "?"
    throttle(ip)
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        _failures.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="Wrong password.")
    _failures.pop(ip, None)
    response.set_cookie(
        COOKIE,
        make_token(),
        max_age=config.ADMIN_SESSION_HOURS * 3600,
        httponly=True,
        secure=ON_RENDER,
        samesite="strict",
    )
    return {"ok": True}


@app.post("/api/admin/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# =============================================================================
# Pages
# =============================================================================
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


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


@app.get("/api/state")
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
        bad_request(DrawError('Type RESET to confirm.'))
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
