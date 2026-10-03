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


# --- Announcement text -------------------------------------------------------
ORG_NAME = "Reverse Draw"            # Name on the post header
ORG_INITIALS = ""                    # Avatar letters; blank = first letters of ORG_NAME
POSTED_IN = "All Company"            # "Posted in ..." line
PRIZE_TEXT = "$10,000 CASH"
CLOSING_NOTE = "Finalists will be announced at the in-person Closing Ceremony."

# --- Draw schedule -----------------------------------------------------------
TOTAL_TICKETS = 1000                 # Tickets are numbered 1..TOTAL_TICKETS

# Tickets still eligible for the grand prize after each stage. Gift-card
# winners leave the pool, which is why each gift-card stage decreases by 1.
ROUND_SURVIVORS = [500, 499, 300, 299, 100, 99, 10]

# One label per stage (same length as ROUND_SURVIVORS).
ROUND_LABELS = [
    "Round 1",
    "Gift Card Draw 1",
    "Round 2",
    "Gift Card Draw 2",
    "Round 3",
    "Gift Card Draw 3",
    "Round 4",
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
]
ROUND_PRIZES = [
    "",
    "Gift Card 1",
    "",
    "Gift Card 2",
    "",
    "Gift Card 3",
    "",
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
PUBLIC_ACCESS_CODE = "123456"
PUBLIC_SESSION_DAYS = 14             # How long a device stays "logged in"
TRADER_SESSION_DAYS = 7              # Holder trading-login session length

# --- Public page -------------------------------------------------------------
PUBLIC_SHOW_HOLDER_NAMES = False     # True = public page can search/show holder names
PUBLIC_SHOW_WINNER_NAME = True       # Show the winner's name in the announcement
PUBLIC_REFRESH_SECONDS = 10          # How often the public page checks for new results

# --- Board look --------------------------------------------------------------
GRID_COLUMNS = 40                    # Squares per row
HIGHLIGHT_LAST_ROUND = True          # Shade the latest eliminations darker

COLOR_ACTIVE = "#2e8b47"             # Still in
COLOR_ELIMINATED = "#dcdfe0"         # Out in an earlier round
COLOR_LAST_ROUND = "#8b949c"         # Out in the most recent round
COLOR_WINNER = "#e8a900"             # Winner(s)
COLOR_PRIZE = "#7651a8"              # Gift-card winner
COLOR_HIGHLIGHT = "#1e6fd9"          # Squares matching the search box

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

# --- Ticket marketplace ------------------------------------------------------
# Marketplace tables may be deployed before the APIs are enabled.
TRADING_ENABLED = _env_bool("TRADING_ENABLED")
TRADING_MIN_PRICE_CENTS = int(os.getenv("TRADING_MIN_PRICE_CENTS", "100"))
TRADING_MAX_PRICE_CENTS = int(os.getenv("TRADING_MAX_PRICE_CENTS", "10000000"))
TRADING_REQUEST_TTL_SECONDS = int(
    os.getenv("TRADING_REQUEST_TTL_SECONDS", str(24 * 60 * 60))
)
TRADING_POLL_SECONDS = int(os.getenv("TRADING_POLL_SECONDS", "5"))
