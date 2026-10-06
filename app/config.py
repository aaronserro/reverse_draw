"""
Reverse Draw - adjustable settings.

This is the only file you normally need to edit. Commit and push to GitHub;
Render redeploys automatically.

Secrets (database URL, admin password) are NOT set here - they are
environment variables in Render. See README.md.
"""

import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise RuntimeError(
            f"{name} must be at least {minimum}{upper}."
        )
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number.") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


# --- Announcement text -------------------------------------------------------
ORG_NAME = "HOOPP Reverse Draw"      # Name on the post header
ORG_INITIALS = "HR"                  # Text fallback when a logo cannot load
POSTED_IN = "United Way Campaign"     # "Posted in ..." line
PRIZE_TEXT = "$10,000 CASH"
CLOSING_NOTE = (
    "Finalists will be announced at the in-person HOOPP United Way "
    "Closing Ceremony."
)

# --- Draw schedule -----------------------------------------------------------
TOTAL_TICKETS = 1000                 # Tickets are numbered 1..TOTAL_TICKETS

# Tickets still eligible for the grand prize after each stage. Gift-card
# winners leave the pool, which is why each gift-card stage decreases by 1.
ROUND_SURVIVORS = [501, 500, 301, 300, 101, 100, 11, 10]

# One label per stage (same length as ROUND_SURVIVORS).
ROUND_LABELS = [
    "Round 1",
    "Gift Card Draw 1",
    "Round 2",
    "Gift Card Draw 2",
    "Round 3",
    "Gift Card Draw 3",
    "Round 4",
    "Gift Card Draw 4",
]

# `elimination` removes the configured number of tickets. `prize` selects the
# one removed ticket as a gift-card winner and records that result separately.
ROUND_KINDS = [
    "elimination",
    "prize",
    "elimination",
    "prize",
    "elimination",
    "prize",
    "elimination",
    "prize",
]
ROUND_PRIZES = [
    "",
    "Gift Card 1",
    "",
    "Gift Card 2",
    "",
    "Gift Card 3",
    "",
    "Gift Card 4",
]

# The draw intentionally stops with finalists; it does not select the grand-
# prize winner in this application.
COMPLETION_LABEL = "Finalist"
COMPLETION_LABEL_PLURAL = "Finalists"

# Completed targets are frozen for audit safety. Future targets and labels
# update dynamically when the completed targets still match; otherwise reset.

# None = a fresh cryptographically random seed each round (logged for audit).
# Set an int only for testing - it makes every draw predictable.
RANDOM_SEED = None

# --- Public access code ------------------------------------------------------
# The 6-digit code people type to see the public page. "" = no code needed.
# If your GitHub repo is public, set the code in Render instead (Environment ->
# PUBLIC_ACCESS_CODE); that value overrides this one.
PUBLIC_ACCESS_CODE = ""
PUBLIC_SESSION_DAYS = 14             # How long a device stays "logged in"
TRADER_SESSION_DAYS = 7              # Holder trading-login session length

# --- Public page -------------------------------------------------------------
PUBLIC_SHOW_HOLDER_NAMES = _env_bool(
    "PUBLIC_SHOW_HOLDER_NAMES", True
)  # Let viewers find people and their tickets
PUBLIC_SHOW_WINNER_NAME = True       # Show the winner's name in the announcement
PUBLIC_REFRESH_SECONDS = 10          # How often the public page checks for new results

# --- Board look --------------------------------------------------------------
GRID_COLUMNS = 40                    # Squares per row
HIGHLIGHT_LAST_ROUND = True          # Shade the latest eliminations darker

COLOR_ACTIVE = "#009944"             # Still in / HOOPP green
COLOR_ELIMINATED = "#dcdfe0"         # Out in an earlier round
COLOR_LAST_ROUND = "#8b949c"         # Out in the most recent round
COLOR_WINNER = "#ffb511"             # Winner(s) / United Way gold
COLOR_PRIZE = "#7651a8"              # Gift-card winner
COLOR_HIGHLIGHT = "#0054a6"          # Search match / United Way blue

# --- Admin -------------------------------------------------------------------
ADMIN_SESSION_HOURS = 12             # How long an admin login lasts

# --- Relational database migration ------------------------------------------
# Leave relational mode disabled until the normalized tables have been
# backfilled and reconciled. The draw ID is required only when this is enabled.
RELATIONAL_STORE_ENABLED = _env_bool("RELATIONAL_STORE_ENABLED")
ACTIVE_DRAW_ID = os.getenv("ACTIVE_DRAW_ID", "").strip()
REQUIRE_DATABASE_IN_PRODUCTION = _env_bool(
    "REQUIRE_DATABASE_IN_PRODUCTION", True
)
MAINTENANCE_MODE = _env_bool("MAINTENANCE_MODE")
OPERATIONAL_STALE_JOB_SECONDS = int(
    os.getenv("OPERATIONAL_STALE_JOB_SECONDS", str(30 * 60))
)
OPERATIONAL_READINESS_MAX_QUERY_MS = _env_int(
    "OPERATIONAL_READINESS_MAX_QUERY_MS", 2000, maximum=300000
)

# Pool capacity is per process. Keep total possible connections below the
# Supabase Session-pooler allowance after multiplying by instances and workers.
DATABASE_POOL_MIN = _env_int("DATABASE_POOL_MIN", 1, maximum=100)
DATABASE_POOL_MAX = _env_int("DATABASE_POOL_MAX", 10, maximum=100)
if DATABASE_POOL_MIN > DATABASE_POOL_MAX:
    raise RuntimeError("DATABASE_POOL_MIN must not exceed DATABASE_POOL_MAX.")
DATABASE_POOL_TIMEOUT_SECONDS = _env_float(
    "DATABASE_POOL_TIMEOUT_SECONDS", 3.0, minimum=0.1, maximum=60.0
)
DATABASE_POOL_STARTUP_TIMEOUT_SECONDS = _env_float(
    "DATABASE_POOL_STARTUP_TIMEOUT_SECONDS",
    30.0,
    minimum=1.0,
    maximum=120.0,
)
DATABASE_STATEMENT_TIMEOUT_MS = _env_int(
    "DATABASE_STATEMENT_TIMEOUT_MS", 5000, maximum=300000
)
DATABASE_LOCK_TIMEOUT_MS = _env_int(
    "DATABASE_LOCK_TIMEOUT_MS", 2000, maximum=300000
)
DATABASE_IDLE_TRANSACTION_TIMEOUT_MS = _env_int(
    "DATABASE_IDLE_TRANSACTION_TIMEOUT_MS", 10000, maximum=300000
)

# --- Ticket marketplace ------------------------------------------------------
# Marketplace tables may be deployed before the APIs are enabled.
TRADING_ENABLED = _env_bool("TRADING_ENABLED")
TRADING_MIN_PRICE_CENTS = int(os.getenv("TRADING_MIN_PRICE_CENTS", "100"))
TRADING_MAX_PRICE_CENTS = int(os.getenv("TRADING_MAX_PRICE_CENTS", "10000000"))
TRADING_REQUEST_TTL_SECONDS = int(
    os.getenv("TRADING_REQUEST_TTL_SECONDS", str(24 * 60 * 60))
)
TRADING_POLL_SECONDS = _env_int("TRADING_POLL_SECONDS", 15, maximum=300)
TRADING_POLL_JITTER_PERCENT = _env_int(
    "TRADING_POLL_JITTER_PERCENT", 20, minimum=0, maximum=50
)
TRADING_POLL_MAX_BACKOFF_SECONDS = _env_int(
    "TRADING_POLL_MAX_BACKOFF_SECONDS", 120, maximum=900
)
TRADING_MARKET_CACHE_SECONDS = _env_float(
    "TRADING_MARKET_CACHE_SECONDS", 1.5, minimum=0.0, maximum=10.0
)
TRADING_HISTORY_LIMIT = _env_int(
    "TRADING_HISTORY_LIMIT", 50, maximum=200
)
