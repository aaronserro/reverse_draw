# Supabase Relational Database Migration Plan

## Purpose

This document describes how to migrate Reverse Draw from one `draw_state.data` JSONB document to normalized tables in the existing Supabase PostgreSQL database.

The migration preserves:

- FastAPI and the existing Render deployment.
- Existing public, administrator, and trader URLs.
- Existing signed-cookie authentication during the initial migration.
- Seeded and auditable reverse-draw behavior.
- Holder import, CSV export, notification, and credential behavior.
- Existing frontend response shapes wherever practical.
- The old `draw_state` row as an immutable cutover backup.

The migration also creates relational foundations for exact-ticket listings, purchase requests, ownership transfers, and a transaction feed. Marketplace behavior is implemented only after the core draw is stable on the relational schema.

> Supabase is PostgreSQL. This is a migration from an aggregate JSONB persistence model to a normalized relational model, not a change of database engine.

---

## 1. Current architecture

The current database has one application row:

```text
draw_state
  id = 1
  data = {
    schedule,
    owners,
    rounds,
    undone,
    source_dataframe,
    allocation_source_fingerprint,
    notification_batches,
    holder_credentials
  }
  updated_at
```

Every write currently performs the equivalent of:

```text
BEGIN
SELECT data FROM draw_state WHERE id = 1 FOR UPDATE
construct ReverseDraw from data
mutate ReverseDraw
serialize the entire ReverseDraw
UPDATE draw_state SET data = complete_new_document
COMMIT
```

This guarantees simple atomic writes but causes every subsystem to share one global lock and one growing JSON document.

### Current source-of-truth locations

| Concern | Current source of truth |
|---|---|
| Schedule | `data.schedule` |
| Ticket holder | `data.owners[ticket_number]` |
| Completed rounds | `data.rounds` |
| Undone rounds | `data.undone` |
| Ticket elimination | Rebuilt from completed rounds |
| Uploaded spreadsheet | Serialized pandas JSON in `data.source_dataframe` |
| Allocation fingerprint | `data.allocation_source_fingerprint` |
| Email batches and jobs | `data.notification_batches` |
| Trader credential metadata | `data.holder_credentials` |
| Admin/public/trader sessions | Signed cookies, not database rows |
| Public cache and rate limits | Process memory, not database rows |
| Listings and trades | Not currently implemented |

---

## 2. Target architecture

```text
Browser
  -> FastAPI
      -> Relational repositories
          -> Supabase PostgreSQL tables
          -> Private Supabase Storage for original imports
```

FastAPI remains the only database client during this migration. Browsers continue calling application APIs; they do not receive a database password or Supabase service-role key.

### Target table groups

```text
Draw configuration
  draws
  draw_stages

Participants and ownership
  draw_participants
  tickets
  ticket_ownership_events
  holder_credentials

Draw execution
  draw_rounds
  round_results

Imports
  import_batches
  import_rows
  private Supabase Storage bucket

Email
  email_batches
  email_jobs
  email_job_tickets

Marketplace
  listings
  purchase_requests
  trades

Operations
  schema_migrations
  data_migrations
```

---

## 3. Migration principles

1. **Preserve behavior before adding behavior.** Move existing draw, ownership, import, email, and authentication behavior first. Add the marketplace afterward.
2. **Do not dual-write indefinitely.** Dual reads are useful for verification, but maintaining two writable sources of truth creates divergence risk.
3. **Keep the existing API contract during cutover.** Build relational projections that produce the payloads expected by the current frontend.
4. **Use explicit database transactions.** Round execution, undo, holder replacement, and trade settlement must each commit atomically.
5. **Give holders stable identities.** Ticket ownership and credentials must reference participant IDs rather than display names.
6. **Retain immutable audit history.** Undone rounds, ownership changes, email outcomes, and settled trades remain queryable.
7. **Require exact reconciliation.** The cutover cannot depend only on matching row counts.
8. **Do not expose private tables directly.** Enable RLS and deny anonymous access until intentional browser policies exist.
9. **Keep prices as integer cents.** Never store marketplace prices using binary floating-point values.
10. **Define the rollback boundary.** Returning to the JSON row is safe only before relational writes begin, unless a tested reverse exporter exists.

---

## 4. Proposed project structure

The implementation should move database responsibilities out of the route module.

```text
reverse_draw/
  app/
    db.py                         # connection pool and transaction helpers
    draw.py                       # pure draw calculations and validation
    main.py                       # HTTP routes and dependency wiring
    repositories/
      __init__.py
      draws.py
      tickets.py
      participants.py
      imports.py
      notifications.py
      marketplace.py
    services/
      draw_service.py
      holder_service.py
      import_service.py
      notification_service.py
      marketplace_service.py
    projections/
      public_state.py
      admin_state.py
      trader_state.py
  migrations/
    001_migration_ledger.sql
    002_draws_and_stages.sql
    003_participants_and_tickets.sql
    004_rounds.sql
    005_imports.sql
    006_notifications.sql
    007_marketplace.sql
    008_rls.sql
  scripts/
    export_legacy_state.py
    migrate_legacy_state.py
    reconcile_legacy_state.py
    reverse_export_relational_state.py   # optional but recommended before cutover
  tests/
    integration/
      test_relational_draws.py
      test_relational_imports.py
      test_relational_notifications.py
      test_relational_marketplace.py
      test_migration_reconciliation.py
```

The exact module split may be adjusted, but route handlers must not accumulate raw SQL for every subsystem.

---

# Phase 0 — Baseline, backup, and guardrails

## Step 0.1 — Capture the legacy state

Export the complete JSON row before changing persistence behavior.

### Pseudocode

```python
function export_legacy_state(database_url, output_directory):
    connect using a direct or session-pooled server connection

    row = query_one(
        "SELECT data, updated_at FROM draw_state WHERE id = 1"
    )

    if row does not exist:
        fail("Legacy draw_state row is missing")

    write_json_atomic(
        output_directory / "draw_state.json",
        {
            "exported_at": utc_now(),
            "legacy_updated_at": row.updated_at,
            "data": row.data,
        },
    )

    draw = ReverseDraw(row.data)
    write_text(output_directory / "holders.csv", draw.owners_csv())
    write_text(output_directory / "tickets.csv", draw.tickets_csv())
    write_text(output_directory / "round-log.csv", draw.log_csv())

    baseline = []
    for ticket_number in draw.tickets():
        baseline.append({
            "ticket": ticket_number,
            "holder": draw.holder(ticket_number),
            "status": draw.status(ticket_number),
            "eliminated_round": draw.eliminated_in.get(ticket_number),
        })

    write_json_atomic(output_directory / "ticket-baseline.json", baseline)
    write_checksum_manifest(output_directory)
```

### Validation

- Confirm the JSON can be parsed back into `ReverseDraw`.
- Confirm tickets span `1..TOTAL_TICKETS`.
- Store the export outside the Render filesystem.
- Keep at least one Supabase database backup in addition to application exports.

## Step 0.2 — Record the existing test baseline

### Pseudocode

```text
run all unit tests
run Python syntax compilation
run JavaScript syntax checks
record command, commit SHA, date, and result
stop migration work if existing tests fail for unrelated reasons
```

## Step 0.3 — Add feature flags

Recommended environment settings:

```text
RELATIONAL_STORE_ENABLED=false
ACTIVE_DRAW_ID=<uuid after migration>
REQUIRE_DATABASE_IN_PRODUCTION=true
```

### Pseudocode

```python
function build_application_storage():
    if running_on_render and DATABASE_URL is empty:
        fail_startup("Production requires DATABASE_URL")

    if RELATIONAL_STORE_ENABLED:
        require ACTIVE_DRAW_ID
        pool = create_postgres_pool(DATABASE_URL)
        verify_schema_version(pool, REQUIRED_SCHEMA_VERSION)
        verify_draw_exists(pool, ACTIVE_DRAW_ID)
        return RelationalRepositories(pool, ACTIVE_DRAW_ID)

    return LegacyStore(DATABASE_URL or SQLITE_PATH)
```

## Step 0.4 — Create migration ledgers

### SQL pseudocode

```sql
CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE data_migrations (
    migration_key TEXT PRIMARY KEY,
    source_updated_at TIMESTAMPTZ,
    source_checksum TEXT NOT NULL,
    target_draw_id UUID NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Pseudocode

```python
function apply_migration(name, sql, checksum):
    existing = query schema_migrations by name

    if existing exists:
        require existing.checksum == checksum
        return "already applied"

    begin transaction
    execute sql
    insert schema_migrations(name, checksum)
    commit
```

---

# Phase 1 — Build the relational schema

## Step 1.1 — Create `draws`

One row represents one event. The initial application still uses one active event, but all dependent tables are draw-scoped.

### SQL pseudocode

```sql
CREATE TABLE draws (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    total_tickets INTEGER NOT NULL CHECK (total_tickets > 0),
    completion_label TEXT NOT NULL,
    completion_label_plural TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'active', 'finished', 'archived')
    ),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`version` replaces hashing the entire serialized document. It increments inside any transaction that changes public or trader-visible state.

## Step 1.2 — Create `draw_stages`

### SQL pseudocode

```sql
CREATE TABLE draw_stages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('elimination', 'prize')),
    prize TEXT NOT NULL DEFAULT '',
    survivor_target INTEGER NOT NULL CHECK (survivor_target > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draw_id, stage_number)
);
```

### Creation pseudocode

```python
function create_draw_from_config(config):
    schedule = current_schedule()
    validate_schedule(schedule)

    begin transaction

    draw_id = insert draws(
        total_tickets=schedule.total,
        completion_label=schedule.completion_label,
        completion_label_plural=schedule.completion_label_plural,
        status="draft",
    )

    previous_target = schedule.total
    for index in range(len(schedule.survivors)):
        target = schedule.survivors[index]
        require 0 < target < previous_target

        insert draw_stages(
            draw_id=draw_id,
            stage_number=index + 1,
            label=schedule.labels[index],
            kind=schedule.kinds[index],
            prize=schedule.prizes[index],
            survivor_target=target,
        )
        previous_target = target

    commit
    return draw_id
```

After cutover, configuration supplies defaults for a new draw. It must not silently rewrite the schedule of an existing draw during application startup.

## Step 1.3 — Create participants and credentials

### SQL pseudocode

```sql
CREATE TABLE draw_participants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    source_email CITEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX one_normalized_participant_per_draw
ON draw_participants(draw_id, normalized_name);

CREATE TABLE holder_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id UUID NOT NULL
        REFERENCES draw_participants(id) ON DELETE CASCADE,
    credential_external_id TEXT NOT NULL UNIQUE,
    code_digest TEXT NOT NULL,
    code_scheme TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at TIMESTAMPTZ,
    UNIQUE (participant_id)
);
```

The first migration preserves the current normalized-name identity rule. If two real people can have the same normalized name, this must be resolved before migration or the identity design must be expanded to include a stable external source ID.

### Participant lookup pseudocode

```python
function get_or_create_participant(draw_id, display_name, optional_email):
    normalized = normalize_person_name(display_name)
    require normalized is not empty

    participant = select participant
                  where draw_id = draw_id
                    and normalized_name = normalized

    if participant exists:
        update display_name and missing source_email if appropriate
        return participant

    return insert participant(
        draw_id=draw_id,
        display_name=clean_display_name(display_name),
        normalized_name=normalized,
        source_email=optional_email,
    )
```

Credentials remain valid when a participant owns zero tickets.

## Step 1.4 — Create tickets and ownership history

### SQL pseudocode

```sql
CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_number INTEGER NOT NULL CHECK (ticket_number > 0),
    owner_participant_id UUID REFERENCES draw_participants(id),
    eliminated_round_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draw_id, ticket_number)
);

CREATE INDEX tickets_by_owner
ON tickets(draw_id, owner_participant_id);

CREATE TABLE ticket_ownership_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    from_participant_id UUID REFERENCES draw_participants(id),
    to_participant_id UUID REFERENCES draw_participants(id),
    reason TEXT NOT NULL CHECK (
        reason IN (
            'initial_import',
            'admin_assignment',
            'admin_unassignment',
            'admin_correction',
            'trade'
        )
    ),
    import_batch_id UUID,
    trade_id UUID,
    actor_type TEXT NOT NULL,
    actor_identifier TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_participant_id IS DISTINCT FROM to_participant_id)
);
```

Foreign keys to import batches and trades are added after those tables exist.

### Initial ticket creation pseudocode

```python
function create_all_tickets(draw_id, total_tickets):
    begin transaction
    lock draw row

    existing_count = count tickets for draw_id
    if existing_count != 0:
        fail("Tickets already initialized")

    bulk_insert tickets for ticket_number in 1..total_tickets
    increment draw.version
    commit
```

## Step 1.5 — Create rounds and results

### SQL pseudocode

```sql
CREATE TABLE draw_rounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    stage_id UUID NOT NULL REFERENCES draw_stages(id),
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    label_snapshot TEXT NOT NULL,
    kind_snapshot TEXT NOT NULL CHECK (
        kind_snapshot IN ('elimination', 'prize')
    ),
    prize_snapshot TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL,
    started_with INTEGER NOT NULL CHECK (started_with > 0),
    survivor_count INTEGER NOT NULL CHECK (survivor_count > 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'undone')),
    executed_at TIMESTAMPTZ NOT NULL,
    undone_at TIMESTAMPTZ,
    undone_by TEXT
);

CREATE UNIQUE INDEX one_completed_execution_per_round
ON draw_rounds(draw_id, round_number)
WHERE status = 'completed';

CREATE TABLE round_results (
    round_id UUID NOT NULL REFERENCES draw_rounds(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    result TEXT NOT NULL CHECK (
        result IN ('eliminated', 'prize_selected')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (round_id, ticket_id)
);

ALTER TABLE tickets
ADD CONSTRAINT tickets_eliminated_round_fk
FOREIGN KEY (eliminated_round_id) REFERENCES draw_rounds(id);
```

The round row stores label, kind, and prize snapshots so later schedule changes do not rewrite audit history.

## Step 1.6 — Create import tables and Storage layout

### SQL pseudocode

```sql
CREATE TABLE import_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    storage_path TEXT,
    source_fingerprint TEXT NOT NULL,
    allocation_fingerprint TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('uploaded', 'previewed', 'applied', 'rejected')
    ),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ
);

CREATE TABLE import_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_batch_id UUID NOT NULL
        REFERENCES import_batches(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    ticket_number INTEGER,
    holder_name TEXT,
    normalized_holder_name TEXT,
    email CITEXT,
    raw_data JSONB NOT NULL DEFAULT '{}',
    validation_error TEXT,
    UNIQUE (import_batch_id, row_number)
);
```

Private Storage path convention:

```text
draw-imports/{draw_id}/{import_batch_id}/{sanitized_filename}
```

### Upload pseudocode

```python
function create_import_preview(draw_id, file_bytes, filename):
    require size(file_bytes) <= MAX_UPLOAD_SIZE
    parsed_frame = parse_csv_or_excel(file_bytes, filename)
    source_fingerprint = fingerprint(parsed_frame)
    parsed_rows = validate_and_normalize_rows(parsed_frame)

    begin transaction
    batch_id = insert import_batches(
        draw_id=draw_id,
        filename=sanitized(filename),
        source_fingerprint=source_fingerprint,
        status="uploaded",
    )
    bulk_insert import_rows(batch_id, parsed_rows)
    update batch status="previewed"
    commit

    upload original file to private Storage
    update batch.storage_path

    return preview derived from import_rows
```

If Storage upload fails after the database insert, mark the batch rejected or retry the upload. Do not apply ownership from an incomplete import.

## Step 1.7 — Create notification tables

### SQL pseudocode

```sql
CREATE TABLE email_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    import_batch_id UUID REFERENCES import_batches(id),
    allocation_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('sending', 'completed', 'attention', 'cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE email_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES email_batches(id) ON DELETE CASCADE,
    participant_id UUID REFERENCES draw_participants(id),
    recipient_name TEXT NOT NULL,
    recipient_email CITEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sent', 'failed', 'unknown', 'cancelled')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    error TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE email_job_tickets (
    job_id UUID NOT NULL REFERENCES email_jobs(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL REFERENCES tickets(id),
    is_new BOOLEAN NOT NULL,
    PRIMARY KEY (job_id, ticket_id)
);

CREATE INDEX email_delivery_lookup
ON email_jobs(recipient_email, status);
```

### Delivery-selection pseudocode

```python
function find_pending_deliveries(draw_id):
    current_allocations = query tickets joined to owners and import email rows

    successful_pairs = query distinct(
        normalized recipient email,
        ticket ID
    ) where email_jobs.status = "sent"

    unknown_pairs = query distinct(
        normalized recipient email,
        ticket ID
    ) where email_jobs.status = "unknown"

    for each recipient allocation:
        new_tickets = allocation tickets
                      minus successful_pairs
                      minus unknown_pairs
        include recipient only if new_tickets is not empty

    return recipient previews
```

## Step 1.8 — Create marketplace tables

### SQL pseudocode

```sql
CREATE TABLE listings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL REFERENCES tickets(id),
    seller_participant_id UUID NOT NULL REFERENCES draw_participants(id),
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL CHECK (
        status IN ('open', 'reserved', 'sold', 'cancelled', 'invalidated')
    ),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX one_active_listing_per_ticket
ON listings(ticket_id)
WHERE status IN ('open', 'reserved');

CREATE TABLE purchase_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id UUID NOT NULL REFERENCES listings(id),
    buyer_participant_id UUID NOT NULL REFERENCES draw_participants(id),
    offered_price_cents BIGINT NOT NULL CHECK (offered_price_cents > 0),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'approved', 'declined',
            'withdrawn', 'expired', 'superseded'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    UNIQUE (buyer_participant_id, idempotency_key)
);

CREATE TABLE trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    listing_id UUID NOT NULL UNIQUE REFERENCES listings(id),
    request_id UUID NOT NULL UNIQUE REFERENCES purchase_requests(id),
    ticket_id UUID NOT NULL REFERENCES tickets(id),
    seller_participant_id UUID NOT NULL REFERENCES draw_participants(id),
    buyer_participant_id UUID NOT NULL REFERENCES draw_participants(id),
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL CHECK (status IN ('settled', 'reversed')),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (seller_participant_id <> buyer_participant_id)
);

ALTER TABLE ticket_ownership_events
ADD CONSTRAINT ownership_event_import_batch_fk
FOREIGN KEY (import_batch_id) REFERENCES import_batches(id);

ALTER TABLE ticket_ownership_events
ADD CONSTRAINT ownership_event_trade_fk
FOREIGN KEY (trade_id) REFERENCES trades(id);
```

## Step 1.9 — Enable RLS with default denial

### SQL pseudocode

```sql
ALTER TABLE draws ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_ownership_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE holder_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_rounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE round_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_job_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
```

Do not create anonymous browser policies in the first migration. FastAPI continues to use the trusted server database connection.

---

# Phase 2 — Build relational repositories and projections

## Step 2.1 — Replace whole-document reads

Repositories should query only the state required by each endpoint.

### Pseudocode

```python
class DrawRepository:
    function get_draw(draw_id): ...
    function get_stages(draw_id): ...
    function get_completed_rounds(draw_id): ...
    function transaction(): ...

class TicketRepository:
    function get_ticket_statuses(draw_id): ...
    function get_holder_tickets(draw_id, participant_id): ...
    function get_holder_summary(draw_id): ...
    function get_ticket_export_rows(draw_id): ...

class ParticipantRepository:
    function get_by_credential_id(credential_id): ...
    function get_or_create_by_normalized_name(...): ...

class NotificationRepository:
    function get_preview_data(draw_id): ...
    function create_batch(...): ...
    function update_job(...): ...
```

## Step 2.2 — Build the public projection

### Pseudocode

```python
function relational_public_payload(draw_id):
    draw = get draw
    stages = get ordered stages
    rounds = get completed rounds ordered by round_number
    ticket_rows = get tickets ordered by ticket_number

    status_array = []
    for ticket in ticket_rows:
        if ticket.eliminated_round_id is null:
            status_array.append(0)
        else:
            status_array.append(ticket.eliminated_round_number)

    winners = []
    if draw.status == "finished":
        winners = tickets where eliminated_round_id is null

    return payload matching current public API shape:
        version = draw.version
        schedule = stages converted to existing arrays
        started = completed round count > 0
        finished = draw.status == "finished"
        rounds_done = completed round count
        next_label = next stage label or null
        status = status_array
        rounds = safe round summaries without seeds
        winners = privacy-filtered winners
        holders = optional privacy-filtered holder map
```

## Step 2.3 — Build administrator and trader projections

### Pseudocode

```python
function relational_admin_payload(draw_id):
    payload = relational_public_payload(draw_id)
    add owner map from tickets + participants
    add completed rounds with seeds and result ticket numbers
    add undone round history
    add holder summary
    add derived trading codes for active credentials
    add storage="supabase-relational"
    return payload

function relational_trader_payload(draw_id, credential_cookie):
    credential = validate signed cookie structure
    database_credential = find active credential by external ID
    verify cookie signature includes current database digest

    participant = database_credential.participant
    tickets = query all tickets currently owned by participant

    return current trader response shape using relational rows
```

A participant is allowed to authenticate with zero tickets.

## Step 2.4 — Replace document hashing

### Pseudocode

```python
function increment_draw_version(connection, draw_id):
    return query_one(
        """
        UPDATE draws
        SET version = version + 1,
            updated_at = now()
        WHERE id = %s
        RETURNING version
        """,
        draw_id,
    )
```

Every visible mutation calls this before commit. The public cache should be keyed by `draw_id + version`, or removed until relational behavior is stable.

---

# Phase 3 — Convert transactional commands

## Step 3.1 — Run the next round

Python keeps responsibility for deterministic seeded selection. PostgreSQL keeps responsibility for concurrency and atomic persistence.

### Pseudocode

```python
function run_next_round(draw_id, expected_version):
    begin transaction with suitable isolation

    draw = SELECT * FROM draws
           WHERE id = draw_id
           FOR UPDATE

    require draw.version == expected_version
    require draw.status not in ("finished", "archived")

    completed_count = count completed rounds for draw
    stage = get stage_number completed_count + 1
    require stage exists

    active_tickets = SELECT tickets
                     WHERE draw_id = draw_id
                       AND eliminated_round_id IS NULL
                     ORDER BY ticket_number

    require count(active_tickets) > stage.survivor_target

    seed = configured deterministic seed or cryptographically random 64-bit seed
    keep = deterministic_sample(active ticket numbers, stage.survivor_target, seed)
    selected = active tickets not in keep

    if stage.kind == "prize":
        require count(selected) == 1
        result_kind = "prize_selected"
    else:
        result_kind = "eliminated"

    round_id = insert draw_rounds using stage snapshots and seed
    bulk_insert round_results(round_id, selected, result_kind)

    update selected tickets
    set eliminated_round_id = round_id

    invalidate open listings and pending requests for selected tickets

    if no later stage exists:
        update draws.status = "finished"
    else:
        update draws.status = "active"

    increment draw version
    commit

    return fresh admin projection
```

### Concurrency requirement

Two requests with the same `expected_version` cannot both complete because both must lock the same draw row and recheck the version after acquiring the lock.

## Step 3.2 — Undo the last completed round

### Pseudocode

```python
function undo_last_round(draw_id, expected_version, admin_identifier):
    begin transaction
    lock draw row
    require draw.version == expected_version

    round = latest completed round ordered by round_number descending
    require round exists

    update round:
        status = "undone"
        undone_at = utc_now()
        undone_by = admin_identifier

    update tickets
    set eliminated_round_id = null
    where eliminated_round_id = round.id

    update draw status based on remaining completed stages

    # Intentionally do not reopen listings or requests that became invalid.
    increment draw version
    commit
```

## Step 3.3 — Replace or merge holder allocation

### Pseudocode

```python
function apply_holder_allocation(draw_id, incoming_mapping, mode, import_batch_id):
    validate every ticket number and holder name before transaction

    begin transaction
    lock draw row
    current = select tickets and current owners for update

    if mode == "replace":
        desired = incoming_mapping
        unlisted tickets have desired owner = null
    else if mode == "merge":
        desired = current mapping overwritten by incoming_mapping
    else:
        fail("Unsupported mode")

    participant_cache = {}
    for each distinct holder name in desired:
        participant_cache[normalized name] = get_or_create_participant(...)
        ensure participant has credential metadata

    changes = compare current owner IDs to desired owner IDs

    for each changed ticket:
        update tickets.owner_participant_id
        insert ticket_ownership_event(
            from=current owner,
            to=desired owner,
            reason based on operation,
            import_batch_id=import_batch_id,
            actor_type="admin",
        )
        invalidate listing if seller no longer owns ticket

    mark import batch applied and save allocation fingerprint
    increment draw version once
    commit
```

## Step 3.4 — Assign or unassign a block

### Pseudocode

```python
function assign_block(draw_id, holder_name, start, end):
    normalized bounds = clamp sorted(start, end) to 1..total_tickets
    require valid bounds and non-empty holder name

    begin transaction
    lock draw row
    participant = get_or_create participant and credential
    lock ticket rows within range

    for each ticket whose owner changes:
        update current owner
        append ownership event
        invalidate incompatible listing

    increment draw version once
    commit
```

Unassignment follows the same structure with `to_participant_id = null`. It does not delete the participant or credential.

## Step 3.5 — Reset

### Pseudocode

```python
function reset_draw(draw_id, keep_holders, destructive_event_reset=false):
    begin transaction
    lock draw row

    mark all completed rounds undone or delete only if policy explicitly permits
    clear tickets.eliminated_round_id
    cancel or invalidate all open listings and pending requests

    if not keep_holders:
        for each assigned ticket:
            append admin_unassignment ownership event
        clear ticket owners
        mark prior participants inactive only if policy requires
        clear credentials only when matching current reset semantics
        detach applied import allocation
        clear notification history only when explicitly requested

    if destructive_event_reset:
        require elevated confirmation
        handle settled trade history according to legal/audit policy

    set draw status="draft"
    increment version
    commit
```

Settled trades should normally be retained even when the draw is reset.

---

# Phase 4 — Convert imports and notifications

## Step 4.1 — Apply an import preview

### Pseudocode

```python
function apply_import_batch(draw_id, batch_id, mode):
    begin transaction
    lock draw row
    batch = select import batch for update

    require batch.draw_id == draw_id
    require batch.status == "previewed"
    require no import rows contain validation errors

    mapping = build ticket -> holder from import rows
    call holder-allocation transaction logic using same connection

    batch.status = "applied"
    batch.applied_at = utc_now()
    batch.allocation_fingerprint = fingerprint(resulting ownership)

    commit
```

Avoid nesting independent transactions. Services should accept an existing connection when composing operations.

## Step 4.2 — Create a notification batch

### Pseudocode

```python
function create_notification_batch(draw_id):
    begin transaction

    expire stale sending batches
    require no active sending batch exists

    preview = compute notification preview using relational queries
    require preview is ready
    require preview recipients is not empty

    batch_id = insert email batch status="sending"

    for recipient in preview.recipients:
        job_id = insert email job status="pending"
        for ticket in recipient.all_tickets:
            insert email_job_ticket(
                job_id=job_id,
                ticket_id=ticket.id,
                is_new=ticket in recipient.new_tickets,
            )

    commit
    enqueue in-process task or durable worker with batch_id only
    return batch_id
```

## Step 4.3 — Process one email job

### Pseudocode

```python
function process_email_job(job_id):
    job = load job, batch, participant, credential, and ticket details
    require job.status == "pending"

    try:
        response = provider.send(render_message(job))
        outcome = {
            status: "sent",
            sent_at: utc_now(),
            provider_request_id: response.request_id,
            error: null,
        }
    except ProviderOutcomeUnknown as error:
        outcome = {status: "unknown", error: truncate(error)}
    except Exception as error:
        outcome = {status: "failed", error: truncate(error)}

    begin short transaction
    lock only the email job row
    if job is still pending:
        update attempt_count and outcome
    update batch completion state from all job statuses
    commit
```

This removes the current need to lock and rewrite unrelated draw state after every email.

---

# Phase 5 — One-time legacy backfill

## Step 5.1 — Validate migration input

### Pseudocode

```python
function validate_legacy_document(data):
    draw = ReverseDraw(data)
    validate_schedule(draw.schedule)

    require owner ticket numbers are unique and in range
    require round numbers are coherent
    require each completed round target matches recorded survivors
    require completed result tickets were active before that round
    require credential values have required fields
    require notification jobs reference valid ticket numbers

    return validated draw and warnings
```

## Step 5.2 — Create the target draw and stages

### Pseudocode

```python
function migrate_schedule(connection, legacy_draw):
    draw_id = insert draws from legacy schedule

    for each schedule index:
        insert draw_stage with ordered number and values

    bulk insert every ticket number from 1..legacy_draw.total
    return draw_id and stage ID map
```

## Step 5.3 — Migrate participants, ownership, and credentials

### Pseudocode

```python
function migrate_owners(connection, draw_id, legacy_draw):
    group legacy owners by normalized holder name

    for each normalized holder:
        if multiple materially different source identities collide:
            fail with ambiguity report

        participant = insert draw participant

        for each owned ticket:
            update ticket owner
            insert initial_import ownership event

        credential = legacy credential for normalized holder
        if credential exists:
            insert relational credential preserving:
                external ID
                digest
                code scheme
                created timestamp

    for each legacy credential without current ownership:
        create or map zero-ticket participant if identity is unambiguous
        insert credential
```

Current production behavior may have pruned zero-ticket credentials. The migration must preserve any credentials that do exist; the relational model stops future pruning.

## Step 5.4 — Migrate completed rounds

### Pseudocode

```python
function migrate_completed_rounds(connection, draw_id, stages, legacy_rounds):
    active_ticket_numbers = set(1..total_tickets)

    for record in legacy_rounds ordered by round:
        require record.round is expected next number
        require record.eliminated is subset of active_ticket_numbers
        require count(active before) == record.started_with
        require count(active before) - count(eliminated) == record.survivors

        round_id = insert completed draw_round using historical snapshots
        result_kind = prize_selected if kind == prize else eliminated
        bulk insert round_results for eliminated ticket IDs
        update matching tickets.eliminated_round_id = round_id

        active_ticket_numbers -= eliminated
```

## Step 5.5 — Migrate undone rounds

### Pseudocode

```python
function migrate_undone_rounds(connection, draw_id, stages, legacy_undone):
    for record in legacy_undone:
        round_id = insert draw_round status="undone"
        insert historical round_results
        do not set tickets.eliminated_round_id
```

## Step 5.6 — Migrate source DataFrame and fingerprints

### Pseudocode

```python
function migrate_source_dataframe(connection, draw_id, source_data, allocation_hash):
    if source_data is null:
        return

    frame = pandas.read_json(source_data.json, orient="split")
    fingerprint = verify or calculate source fingerprint

    batch_id = insert import_batch(
        filename=source_data.filename,
        source_fingerprint=fingerprint,
        allocation_fingerprint=allocation_hash,
        status="applied" if allocation_hash exists else "previewed",
        uploaded_at=source_data.uploaded_at,
    )

    for row number and row in frame:
        parse known ticket, name, and email columns
        insert import_row with raw_data preserving remaining columns
```

If the original binary file is unavailable, leave `storage_path` null and record that the batch was migrated from serialized legacy data.

## Step 5.7 — Migrate notifications

### Pseudocode

```python
function migrate_notifications(connection, draw_id, legacy_batches):
    for legacy batch:
        batch_id = insert email_batch preserving IDs where practical

        for legacy job:
            participant = resolve by normalized name and/or email
            job_id = insert email_job preserving outcome fields

            for ticket number in job.all_tickets:
                ticket_id = resolve draw ticket
                insert email_job_ticket(
                    is_new = ticket number in job.new_tickets
                )
```

## Step 5.8 — Make migration idempotent

### Pseudocode

```python
function migrate_legacy_export(export, migration_key, target_draw_id):
    checksum = sha256(canonical_json(export.data))

    existing = select data_migration by migration_key
    if existing exists:
        require existing.source_checksum == checksum
        return existing.details

    begin transaction
    lock an application-level advisory migration key
    recheck migration ledger

    validate source
    insert all relational state
    run in-transaction structural checks
    insert data_migrations record with checksum and summary
    commit

    return migration summary
```

Do not use “insert and ignore conflicts” broadly. Unexpected conflicts should fail the migration rather than silently producing partial state.

---

# Phase 6 — Reconciliation

## Step 6.1 — Produce legacy and relational snapshots

### Pseudocode

```python
function build_legacy_snapshot(data):
    draw = ReverseDraw(data)
    return canonical snapshot containing:
        schedule
        ticket -> owner
        ticket -> status
        ticket -> eliminated round
        completed round records and result sets
        undone round records and result sets
        credentials by normalized holder
        source and allocation fingerprints
        notification batches/jobs/tickets

function build_relational_snapshot(draw_id):
    query and normalize the same information from relational tables
    return the same canonical snapshot shape
```

## Step 6.2 — Compare exact values

### Pseudocode

```python
function reconcile(legacy_snapshot, relational_snapshot):
    differences = []

    compare schedule stage-by-stage
    compare every ticket from 1..total
    compare owner, status, and eliminated round per ticket
    compare holder ticket sets
    compare completed and undone round metadata
    compare each round result set without relying on row order
    compare credential IDs, schemes, and digests
    compare fingerprints
    compare email batch/job statuses and delivery ticket sets

    write human-readable and machine-readable reports

    if differences is not empty:
        fail cutover
```

### Required invariants

```text
number of tickets = draws.total_tickets
distinct ticket numbers = draws.total_tickets
completed rounds have one unique round number each
active tickets + currently eliminated tickets = total tickets
current ticket elimination references point only to completed rounds
undone rounds do not control current ticket status
current owner participant belongs to the same draw
successful email delivery pairs match legacy behavior
credential digests are unchanged during migration
```

---

# Phase 7 — Cutover

## Step 7.1 — Pre-cutover deployment

### Pseudocode

```text
apply schema migrations to production
set RELATIONAL_STORE_ENABLED=false
deploy code capable of both legacy and relational reads
run health check and legacy smoke tests
confirm the production backup is restorable
require the disposable PostgreSQL test suite to pass
```

## Step 7.2 — Maintenance-window backfill

### Pseudocode

```text
enable maintenance mode that rejects writes
wait for active background email work to finish or cancel it safely
take final Supabase backup
export final draw_state row and checksums
run idempotent legacy migration
run exact reconciliation
compare legacy and relational public/admin/trader responses
if any unexplained mismatch exists:
    keep relational mode disabled
    leave writes disabled until legacy safety is confirmed
```

## Step 7.3 — Enable relational mode

### Pseudocode

```text
set ACTIVE_DRAW_ID to migrated draw UUID
set RELATIONAL_STORE_ENABLED=true
restart Render service
verify schema version and active draw startup checks
run read-only smoke tests
run controlled production mutation tests while maintenance remains enabled
re-enable production writes
record cutover timestamp and source checksum
```

## Step 7.4 — Production smoke test

Verify:

1. Public board status and refresh.
2. Administrator login and state.
3. Trader login and holdings.
4. Holder import preview and application.
5. Holder, ticket, and round-log exports.
6. Email preview and delivery history.
7. Round execution conflict protection.
8. Undo behavior.
9. Restart persistence.
10. No anonymous access to participants, credentials, imports, or email data.

## Step 7.5 — Rollback rules

### Pseudocode

```text
IF relational mode has not accepted any write:
    disable relational flag
    restart application
    resume from unchanged legacy row

ELSE IF relational writes have occurred:
    block writes
    do not enable stale legacy mode
    repair forward OR run tested reverse exporter
    reconcile reverse export before switching
```

The legacy row is not a live rollback source after relational writes begin because it no longer receives updates.

---

# Phase 8 — Implement marketplace behavior

## Step 8.1 — Create or update a listing

### Pseudocode

```python
function upsert_listing(draw_id, participant_id, ticket_number, price_cents):
    require configured minimum <= price_cents <= configured maximum

    begin transaction
    lock draw row
    lock ticket row by draw and ticket number

    require draw is active and unfinished
    require ticket.owner_participant_id == participant_id
    require ticket.eliminated_round_id is null

    active_listing = find active listing for ticket for update

    if active_listing exists:
        require active_listing.seller == participant_id
        update price, version, and timestamp
    else:
        insert open listing

    increment draw version
    commit
```

## Step 8.2 — Create a purchase request

### Pseudocode

```python
function request_purchase(listing_id, buyer_id, idempotency_key):
    begin transaction

    existing = find request by buyer + idempotency key
    if existing exists:
        return existing

    listing = select listing for update
    ticket = select listing ticket

    require listing.status == "open"
    require listing.seller != buyer_id
    require ticket.owner == listing.seller
    require ticket is active

    request = insert purchase request(
        offered_price_cents=listing.price_cents,
        status="pending",
        idempotency_key=idempotency_key,
    )

    commit
    return request
```

## Step 8.3 — Approve and settle a trade

### Pseudocode

```python
function approve_purchase_request(request_id, seller_id):
    begin transaction

    request = select request for update
    listing = select request listing for update
    ticket = select listing ticket for update
    draw = select listing draw for update

    require request.status == "pending"
    require listing.status in ("open", "reserved")
    require listing.seller_participant_id == seller_id
    require ticket.owner_participant_id == seller_id
    require ticket.eliminated_round_id is null
    require request.buyer_participant_id != seller_id
    require draw.status == "active"

    trade_id = insert trade using snapshotted request price

    update ticket owner = request.buyer

    insert ownership event:
        from = seller
        to = buyer
        reason = "trade"
        trade_id = trade_id

    update accepted request status="approved"
    update listing status="sold", closed_at=now()
    update competing pending requests status="superseded"
    increment draw version

    commit
    return fresh trading snapshot
```

The locks and revalidation make simultaneous approvals safe. Only one transaction can observe and settle a valid current owner/listing/request combination.

## Step 8.4 — Build the transaction feed

### Pseudocode

```python
function get_trading_snapshot(draw_id, participant_id):
    return {
        holdings: current tickets owned by participant,
        open_listings: active eligible listings,
        own_listings: listings created by participant,
        incoming_requests: pending requests for participant listings,
        outgoing_requests: participant purchase requests,
        low_ask: minimum open listing price,
        last_trade: most recent settled trade,
        feed: recent settled trades joined to buyer, seller, and ticket,
        draw_version: current draw version,
        server_time: utc_now(),
    }
```

The frontend should poll every 3–5 seconds and refresh immediately after focus, reconnect, or a successful mutation. Supabase Realtime can later invalidate snapshots, but it is not required for correctness.

---

# Phase 9 — Testing strategy

## Unit tests

```text
schedule validation
seeded ticket selection
round result validation
participant name normalization
credential derivation and cookie validation
public/admin/trader projection formatting
import-row parsing and fingerprints
notification deduplication
marketplace state validation
```

## Repository integration tests

Run these against a disposable PostgreSQL/Supabase database rather than SQLite.

### Pseudocode

```python
before test session:
    create isolated schema or database
    apply every migration in order

after each test:
    truncate application tables in dependency-safe order

after test session:
    remove isolated schema or database
```

Validate:

- Foreign keys.
- Check constraints.
- Partial unique indexes.
- Transaction rollback.
- RLS denial for anonymous roles.
- Connection behavior through the Supabase session pooler.

## Concurrency tests

### Duplicate round test

```python
start two transactions with the same expected draw version
submit next-round command concurrently
assert exactly one commits
assert exactly one completed round exists
assert the other receives a conflict
```

### Competing trade approvals

```python
create one ticket listing
create two pending purchase requests
approve both concurrently
assert one trade exists
assert one request is approved
assert the other is superseded or conflicts
assert ticket has exactly one buyer
assert one ownership event records the transfer
```

### Concurrent email updates

```python
create multiple pending jobs in one batch
process jobs concurrently
assert each job reaches one terminal state
assert unrelated draw rows were not locked or modified
assert final batch status is correct
```

## Migration tests

```text
migrate a realistic legacy fixture
run reconciliation and require no differences
run migration again and require no duplicate data
inject each expected malformed state and require safe failure
verify transaction rollback leaves no partial target draw
```

---

# Phase 10 — Operations and monitoring

Monitor:

- API request latency by endpoint.
- PostgreSQL pool saturation.
- Transaction duration and lock waits.
- Failed expected-version checks.
- Round and trade transaction failures.
- Email jobs stuck in `pending` or `unknown`.
- Database and Storage growth.
- Failed migration or schema-version checks.
- Public projection cache hit rate if caching remains enabled.

### Operational health pseudocode

```python
function health_check():
    verify database query succeeds
    verify required schema version exists
    verify active draw exists
    verify ticket count equals draw.total_tickets
    verify no ticket points to an undone round

    return healthy only if required production invariants pass
```

Do not put secret values, credential digests, email addresses, or source spreadsheet rows into application logs.

---

# File-by-file implementation impact

## `app/db.py`

- Retain Supabase-compatible Psycopg pool configuration.
- Stop creating `draw_state` automatically in relational mode.
- Add transaction helpers and repository wiring.
- Keep the legacy store only during migration stabilization.

## `app/draw.py`

- Keep schedule validation, status calculation, and seeded selection logic.
- Introduce calculation inputs that do not require constructing one full persisted document.
- Stop treating `to_dict()` as the relational persistence contract.
- Keep legacy deserialization until migration fixtures and reconciliation no longer need it.

## `app/main.py`

- Replace `load()` and `Mutation` usage endpoint-by-endpoint.
- Route reads through relational projections.
- Route writes through explicit command services and transactions.
- Update startup credential synchronization so it does not mutate legacy state in relational mode.
- Replace document-cache invalidation with draw-version-aware behavior.

## `app/config.py`

Add:

```text
RELATIONAL_STORE_ENABLED
ACTIVE_DRAW_ID
TRADING_ENABLED
TRADING_MIN_PRICE_CENTS
TRADING_MAX_PRICE_CENTS
TRADING_REQUEST_TTL_SECONDS
TRADING_POLL_SECONDS
```

The configured schedule becomes a default for creating a draw. Existing relational draws use their persisted stages.

## `app/email_service.py`

- Keep SMTP/Graph providers and rendering.
- Accept relational job and credential data.
- Avoid database access inside provider clients.

## `render.yaml`

Add or document:

```text
RELATIONAL_STORE_ENABLED=false during deployment
ACTIVE_DRAW_ID after migration
Supabase Storage credentials only if server-side Storage API is used
```

Do not expose these values to browser JavaScript.

## Static trading files

After normalized marketplace APIs exist:

- Render live listings and requests.
- Add listing/request mutation controls.
- Poll an authoritative server snapshot.
- Bump the service-worker cache version.

---

# Deployment checklist

## Before schema deployment

- [ ] Legacy JSON export stored safely.
- [ ] Supabase backup confirmed.
- [ ] Existing tests passing.
- [ ] Schema migrations tested in order on an empty database.
- [ ] RLS default denial verified.

## Before backfill

- [ ] Idempotent migration command tested twice.
- [ ] Production reconciliation report has zero differences.
- [ ] Ambiguous holder-name collisions resolved.
- [ ] Notification delivery pairs verified.
- [ ] Credentials reproduce the same readable codes using the existing `SECRET_KEY`.
- [ ] Maintenance mode tested.

## Before enabling relational writes

- [ ] Final backup and export complete.
- [ ] Final backfill complete.
- [ ] Public/admin/trader projections match.
- [ ] Ticket-level reconciliation has zero differences.
- [ ] Active draw ID configured.
- [ ] Render restart succeeds.
- [ ] Production health check passes.

## After cutover

- [ ] Public board verified on a separate device.
- [ ] Admin state and exports verified.
- [ ] Trader login and holdings verified.
- [ ] Import and email preview verified.
- [ ] Lock and expected-version conflict behavior verified.
- [ ] Database metrics monitored.
- [ ] Legacy row marked immutable.
- [ ] Backup restore drill completed before legacy removal.

---

# Acceptance criteria

The migration is complete only when:

1. No normal runtime endpoint reads or writes `draw_state.data`.
2. Existing public, administrator, trader, and CSV behavior remains compatible unless explicitly versioned.
3. Every round mutation is atomic and auditable.
4. Every ownership mutation is atomic and has an ownership event.
5. Zero-ticket participants retain their credential and can authenticate.
6. Email history and deduplication no longer require scanning a complete application document.
7. The exact reconciliation report contains no unexplained differences.
8. Anonymous browser roles cannot read credentials, imports, email jobs, or private holder data.
9. Marketplace settlement produces one owner, one trade, and one ownership event in a single transaction.
10. The legacy row remains an immutable backup until the stabilization and restore period is complete.

---

# Explicit exclusions

The initial migration does not include:

- A Django rewrite.
- Immediate Supabase Auth adoption.
- Direct browser writes to Supabase tables.
- Payments, escrow, refunds, or financial custody.
- Automatic order matching or a continuous bid/ask exchange.
- Redis or a durable external email worker unless separately scheduled.
- Automatic rollback to the legacy JSON row after relational writes have begun.

These can be added later without changing the normalized ownership, round, notification, and marketplace foundations described above.
