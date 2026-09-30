"""
Reverse Draw - adjustable settings.

This is the only file you normally need to edit. Commit and push to GitHub;
Render redeploys automatically.

Secrets (database URL, admin password) are NOT set here - they are
environment variables in Render. See README.md.
"""

# --- Announcement text -------------------------------------------------------
ORG_NAME = "Reverse Draw"            # Name on the post header
ORG_INITIALS = ""                    # Avatar letters; blank = first letters of ORG_NAME
POSTED_IN = "All Company"            # "Posted in ..." line
PRIZE_TEXT = "$8,000 CASH"
CLOSING_NOTE = "Finalists will be announced at the in-person Closing Ceremony."

# --- Draw schedule -----------------------------------------------------------
TOTAL_TICKETS = 1000                 # Tickets are numbered 1..TOTAL_TICKETS

# Tickets still in after each round. Must be strictly decreasing.
# The last number is how many winners there are (usually 1).
ROUND_SURVIVORS = [500, 301, 102, 89, 13]

# One label per round (same length as ROUND_SURVIVORS).
ROUND_LABELS = ["Round 1", "Round 2", "Round 3", "Round 4", "Grand Prize"]

# Completed round targets are frozen for audit safety. Future targets and labels
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
COLOR_HIGHLIGHT = "#1e6fd9"          # Squares matching the search box

# --- Admin -------------------------------------------------------------------
ADMIN_SESSION_HOURS = 12             # How long an admin login lasts
