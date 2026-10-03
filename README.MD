# Reverse Draw

A web app for running a reverse draw. It starts with 1000 tickets, includes three gift-card draws, and stops with 10 grand-prize finalists.

- **Public page (`/`)** opens on a landing screen that asks for the 6-digit access code. After that it shows the announcement band, the headline counts, the ticket board, an "Am I still in?" lookup, the round list and a short "How it works". It picks up new results every 10 seconds.
- **Admin page (`/admin`)** needs a password. From it you run, undo and reset rounds, manage ticket holders, view the elimination log and download CSV files.
- **Trading page (`/trading`)** provides secure holder login, current holdings,
  exact-ticket listings, purchase requests, seller approval, ownership transfer,
  and a settled transaction feed. It records prices but does not process money,
  escrow, or refunds.

The stack is FastAPI (Python), plain HTML/CSS/JS and Postgres (Supabase). Everything runs as one Render web service.

## Project layout

```
app/
  config.py       adjustable settings and relational feature flags
  draw.py         persistence-independent draw calculations
  db.py           legacy and normalized PostgreSQL connection helpers
  relational_api.py
  repositories/   normalized table access
  services/       transactional commands
  projections/    public, administrator, trader, and market payloads
  static/         frontend
migrations/       ordered, checksummed PostgreSQL migrations
scripts/          export, backfill, reconciliation, and health commands
tests/            unit and PostgreSQL integration tests
OPERATIONS.md     production migration and incident runbook
render.yaml       Render blueprint
```

## Run it on your computer

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env   -> then edit ADMIN_PASSWORD
uvicorn app.main:app --reload
```

Then open http://localhost:8000 for the public page and http://localhost:8000/admin for the admin page.

When `DATABASE_URL` is empty, the app saves everything to a local `reverse_draw.db` file. That file is only for testing on your own machine.

## Public access code
The code people type to see the board is `PUBLIC_ACCESS_CODE` in `app/config.py` (default `123456`, so change it). If your GitHub repo is public, anyone can read that file. In that case, add `PUBLIC_ACCESS_CODE` as an environment variable in Render instead; it overrides the value in `config.py`. Changing the code signs everyone out, and admins never need it. Set it to `""` to turn the code off.

## Change the settings

Edit `app/config.py`, then commit and push. Render redeploys automatically.
Future rounds update dynamically when every completed round still has the same
survivor target. Completed results remain frozen for audit safety; changing a
completed target requires an admin reset.

## Deploy (GitHub + Supabase + Render)

### 1. Database (Supabase)
1. Create a project at supabase.com and save the database password it gives you.
2. Click **Connect** at the top of the project, then copy the **Session pooler** connection string. Don't use "Direct connection": that one is IPv6-only, and Render can't reach it.
3. In the string, replace `[YOUR-PASSWORD]` with your database password.

Existing deployments may still have the legacy `draw_state` row. Normalized
tables are created only by the ordered migration runner; application startup
does not create them. Do not enable relational storage until migration,
backfill, and exact reconciliation have succeeded.

### 2. GitHub
```
git remote add origin https://github.com/<you>/reverse-draw.git
git push -u origin main
```
(Create the empty `reverse-draw` repo on GitHub first. Don't add a README there.)

### 3. Render
1. In Render, go to **New → Blueprint** and choose your repo. Render reads `render.yaml`.
2. When it asks for environment variables:
   - `DATABASE_URL`: the Supabase Session pooler string from step 1
   - `ADMIN_PASSWORD`: the password for `/admin`
   - `SECRET_KEY` is generated for you automatically.
3. Deploy. Your site will be at `https://reverse-draw-XXXX.onrender.com`.

After that, every push to `main` redeploys the site.

## Production relational migration

This project uses a guarded one-shot cutover to its existing Supabase and Render
production environment. Follow [OPERATIONS.md](OPERATIONS.md) exactly. The
required high-level order is:

1. Deploy with `RELATIONAL_STORE_ENABLED=false`, `TRADING_ENABLED=false`, and
  `MAINTENANCE_MODE=true`.
2. Confirm a restorable backup, export the legacy row, and retain its checksum
  outside Render.
3. Apply every migration with `python -m scripts.apply_migrations`.
4. Run the idempotent backfill into a predetermined draw UUID.
5. Run exact reconciliation and require zero differences.
6. Enable relational mode while maintenance remains active and require
  `/healthz` HTTP 200.
7. Smoke-test public, admin, trader, import, notification, and marketplace
  behavior before reopening writes and enabling trading.

After relational writes begin, the legacy row is stale and must not be used as
an automatic rollback source.

Original spreadsheet binaries are not uploaded to Supabase Storage in the
initial relational release. Operators must retain each source file in approved
encrypted organizational storage. PostgreSQL retains normalized import rows,
fingerprints, validation results, and allocation history.

## Ticket emails on paid Render

The **Email tickets** admin tab reviews everyone with new ticket allocations and
sends one private, personalized message per recipient. A successful
`(email address, ticket number)` delivery is stored in the database, so uploading
the same complete export again does not send duplicates. If a returning buyer
gets more tickets, their next message highlights only the new tickets and also
lists all of their current tickets.

Sending is always manual: upload the complete spreadsheet, review and Save or
Merge the allocation, open **Email tickets**, review the recipient list, and
choose **Send all pending emails**.

Free Render services block outbound ports 25, 465 and 587. This Blueprint uses
a Starter Render instance so the application can connect directly to Gmail with
Python's built-in SMTP client. No Microsoft tenant or additional email service
is required.

### 1. Create the Gmail app password

1. Enable 2-Step Verification on the Gmail sender account.
2. Open the Google Account **App passwords** page.
3. Create an app password named `Reverse Draw`.
4. Copy the generated 16-character password, remove its display spaces, and
   keep it out of GitHub.

### 2. Set the Render environment variables

Use these exact names in **Render → reverse-draw → Environment**:

```dotenv
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your.sender.account@gmail.com
SMTP_PASSWORD=the-16-character-google-app-password
EMAIL_SENDER_ADDRESS=your.sender.account@gmail.com
EMAIL_SENDER_NAME=United Way Reverse Draw
PUBLIC_APP_URL=https://reverse-draw-xxxx.onrender.com
```

- `EMAIL_PROVIDER`: exactly `smtp`.
- `SMTP_HOST`: exactly `smtp.gmail.com`.
- `SMTP_PORT`: exactly `587`.
- `SMTP_SECURITY`: exactly `starttls`.
- `SMTP_USERNAME`: the complete Gmail sender address.
- `SMTP_PASSWORD`: the Google app password with all spaces removed.
- `EMAIL_SENDER_ADDRESS`: normally the same value as `SMTP_USERNAME`.
- `EMAIL_SENDER_NAME`: the changeable display name recipients see, such as
  `United Way Reverse Draw`. It can be changed without changing credentials.
- `PUBLIC_APP_URL`: the deployed site's full `https://` origin, with no trailing
  slash and no `/admin` path.

Confirm the reverse-draw service's instance type is **Starter** or higher, save
the environment, and redeploy. The admin panel shows only the configured sender
and the pending personalized messages. If an earlier attempt is still marked
unknown, check Gmail Sent before choosing **Mark delivered** or **Allow retry**.

### Corporate junk-filter checklist

No application can force a receiving mail system to use the inbox. For the best
delivery results:

- `EMAIL_SENDER_ADDRESS` and `SMTP_USERNAME` should be exactly the same Gmail
  address unless the From address is a verified Gmail alias.
- Use an established sender account with a recognizable display name. Avoid
  sending the entire list repeatedly during testing.
- Ask test recipients to choose **Not junk** and add the sender to their
  contacts or safe-senders list.
- If the whole organization quarantines the messages, ask its mail administrator
  to allow the exact sender address. That policy cannot be bypassed in code.
- For the strongest long-term authentication and brand alignment, send from a
  Google Workspace mailbox on an organization-owned domain with SPF, DKIM, and
  DMARC configured for that domain.

The message contains plain-text and HTML versions, has no tracking pixels, and
adds standard Date, Message-ID, and Reply-To headers. Gmail supplies the actual
SPF/DKIM authentication when it accepts the message.

Microsoft Graph remains supported for existing deployments by setting
`EMAIL_PROVIDER=graph` together with `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`,
`MS_GRAPH_CLIENT_SECRET`, `EMAIL_SENDER_ADDRESS`, and `PUBLIC_APP_URL`.

### 4. Before the event
- Confirm the web service is using the Starter instance declared in `render.yaml`.
- Do a practice run: load the holders, run every round, then **Reset** (keep holders ticked).
- If the admin page ever shows a red "No database is connected" banner, `DATABASE_URL` isn't set, and nothing will survive a restart.

## How the draw works
- The seven draw stages remove `500 → 1 → 199 → 1 → 199 → 1 → 89`
  tickets, leaving exactly 10 finalists.
- Each one-ticket stage selects a gift-card winner. That ticket wins its gift
  card and leaves the grand-prize pool before the next elimination stage.
- Every selection is uniform and uses a fresh cryptographic seed saved in the
  audit log. The application stops after confirming the 10 finalists.
- In relational mode, draw configuration, tickets, rounds, imports,
  notifications, listings, requests, trades, and audit events are stored in
  normalized Supabase PostgreSQL tables. Draw-level row locking and expected
  versions prevent duplicate round execution.
- **Undo** reverses the last stage. For a gift-card stage, it also removes that
  recorded gift-card result. Undone stages remain in the audit trail.
- By default, holder names are **not** exposed on the public page. The public
  lookup works by ticket number only, and finalist names appear in the final
  announcement. To change this, use `PUBLIC_SHOW_HOLDER_NAMES` and
  `PUBLIC_SHOW_WINNER_NAME` in `config.py`.

## Holder CSV format
```
ticket,name
1,Jane Doe
2,"Smith, Bob"
```
Paste the list or load a file on the admin **Ticket holders** tab. **Save** replaces every holder with the list. **Merge** only adds or overwrites the tickets in the list.

## Holder trading login codes

Every normalized holder name has one six-digit login code, even when that person
owns multiple tickets. New codes are generated when tickets are assigned. The
admin **By person** tab displays every current code and can download them as a
CSV. Personalized ticket emails also include the holder's code and a link to the
trading login. The readable value is reproducibly derived using `SECRET_KEY` and
is never stored in the database.

Resetting a holder's code immediately invalidates both the old code and existing
trading sessions. In relational mode, a zero-ticket participant retains their
credential and can authenticate. A draw reset preserves credentials when
**Keep ticket holders** is selected. Sign-in failures use a generic message and
are rate-limited by address and holder.
