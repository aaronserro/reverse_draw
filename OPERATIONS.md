# Relational Operations and Cutover Runbook

## Scope and safety rules

This runbook covers the normalized PostgreSQL runtime. FastAPI is the only database client. Never put database URLs, cookies, access codes, credential digests, email addresses, spreadsheet rows, or provider credentials in tickets, command output, screenshots, or logs.

After relational writes begin, the legacy `draw_state` row is an immutable backup—not a live rollback target. Restoring legacy mode after new rounds or ownership transfers would discard committed audit history.

## Required environment

- `DATABASE_URL`: Supabase Session pooler URL used only by the server.
- `ACTIVE_DRAW_ID`: UUID produced by the verified backfill.
- `RELATIONAL_STORE_ENABLED`: remains `false` until the final cutover.
- `MAINTENANCE_MODE`: blocks writes while leaving reads available.
- `TRADING_ENABLED`: enabled only after ownership and marketplace smoke tests.
- `OPERATIONAL_STALE_JOB_SECONDS`: readiness threshold; default 1800.
- Stable `SECRET_KEY`: required to preserve credential derivation and sessions.

Do not expose any of these values to browser JavaScript or commit them to the repository.

## Single-production-environment policy

This project uses one Supabase production project and one Render production
service. There is no persistent staging environment. The cutover is therefore
performed once against production, with these mandatory safeguards:

1. Complete the disposable local PostgreSQL test suite before touching
  production.
2. Confirm a restorable Supabase backup and retain a final legacy export
  outside Render's filesystem.
3. Deploy with relational storage and trading disabled.
4. Enable maintenance before the final export and keep it enabled through
  migration, backfill, reconciliation, and smoke testing.
5. Reopen writes only after `/healthz`, the operational check, and exact
  reconciliation all pass.

Enter every secret directly in Render. Do not transmit API keys or passwords
through chat, source control, screenshots, or support tickets.

## Import source-file retention

The initial relational release intentionally does **not** copy original CSV or
Excel binaries to Supabase Storage. PostgreSQL is authoritative for normalized
rows, row validation, fingerprints, allocation results, and import audit data;
it is not an archive of the uploaded binary.

Before importing, the operator must place the original file in approved,
encrypted organizational storage with access logging and the organization's
normal records-retention policy. Preserve the file under an immutable import
identifier or with the recorded fingerprint so it can be correlated with the
database batch. Never rely on Render's ephemeral filesystem. Adding Supabase
Storage later requires a private bucket, server-only credentials, object
checksums, retry/idempotency behavior, deletion policy, and restore tests; a
nullable storage path alone is not evidence that a binary was retained.

## Monitoring and alert thresholds

Monitor the following in Render and Supabase:

| Signal | Warning | Critical/action |
| --- | --- | --- |
| `/healthz` | One 503 | Persistent 503 for two checks; keep writes disabled and inspect checks |
| API latency | p95 above 500 ms for 5 minutes | p95 above 2 seconds or rising lock waits |
| Pool pressure | `requests_waiting > 0` repeatedly | Sustained waiters; reduce worker concurrency or increase pool capacity safely |
| Expected-version conflicts | Sudden increase | Check duplicate clients, polling, or stale deployments |
| HTTP 5xx | Any repeated route failure | Set maintenance mode if writes may be affected |
| PostgreSQL lock waits | Wait over 5 seconds | Identify blocker; do not terminate a transaction until its purpose is known |
| Email jobs | Pending beyond configured threshold | Resolve provider/worker state before retrying |
| Unknown email outcome | Any | Reconcile with provider before manually resolving |
| Storage growth | Unexpected daily increase | Inspect imports, email history, trades, and audit retention |
| Migration checks | Missing version or checksum mismatch | Stop deployment; never rewrite an applied migration |

Structured request logs contain only method, route template, status, latency, storage mode, conflict classification, and failure classification. They intentionally omit URLs, query strings, headers, bodies, identities, and exception text.

## Automated readiness check

Run from the application environment so secrets remain environment variables:

```sh
python -m scripts.operational_check
```

The command prints one JSON object and exits 0 only when:

- The required migration version is present.
- The active draw exists.
- The ticket count equals `draws.total_tickets`.
- No ticket references an undone round.
- No pending email job is older than the configured threshold.
- PostgreSQL queries succeed.

The report includes sanitized pool counters. It never includes connection details or private row values.

## Diagnostic SQL

Replace `:active_draw_id` in a protected Supabase SQL session. Do not paste query results containing private columns into external systems.

### Migration status

```sql
SELECT version, applied_at
FROM schema_migrations
ORDER BY version;

SELECT migration_key, target_draw_id, completed_at
FROM data_migrations
WHERE target_draw_id = ':active_draw_id'::uuid;
```

### Ticket and round invariants

```sql
SELECT d.total_tickets, count(t.id) AS actual_tickets
FROM draws d
LEFT JOIN tickets t ON t.draw_id = d.id
WHERE d.id = ':active_draw_id'::uuid
GROUP BY d.total_tickets;

SELECT t.ticket_number, r.round_number, r.status
FROM tickets t
JOIN draw_rounds r ON r.id = t.eliminated_round_id
WHERE t.draw_id = ':active_draw_id'::uuid
  AND r.status <> 'completed';
```

### Lock waits

```sql
SELECT waiting.pid AS waiting_pid,
       waiting.wait_event_type,
       waiting.wait_event,
       blocker.pid AS blocking_pid,
       age(clock_timestamp(), waiting.query_start) AS waiting_for
FROM pg_stat_activity waiting
JOIN pg_locks waiting_lock ON waiting_lock.pid = waiting.pid
JOIN pg_locks blocker_lock
  ON blocker_lock.locktype = waiting_lock.locktype
 AND blocker_lock.database IS NOT DISTINCT FROM waiting_lock.database
 AND blocker_lock.relation IS NOT DISTINCT FROM waiting_lock.relation
 AND blocker_lock.page IS NOT DISTINCT FROM waiting_lock.page
 AND blocker_lock.tuple IS NOT DISTINCT FROM waiting_lock.tuple
 AND blocker_lock.transactionid IS NOT DISTINCT FROM waiting_lock.transactionid
 AND blocker_lock.classid IS NOT DISTINCT FROM waiting_lock.classid
 AND blocker_lock.objid IS NOT DISTINCT FROM waiting_lock.objid
 AND blocker_lock.objsubid IS NOT DISTINCT FROM waiting_lock.objsubid
 AND blocker_lock.pid <> waiting_lock.pid
JOIN pg_stat_activity blocker ON blocker.pid = blocker_lock.pid
WHERE NOT waiting_lock.granted AND blocker_lock.granted;
```

### Stuck notification work

```sql
SELECT b.id AS batch_id, b.status, count(*) AS pending_jobs,
       min(j.created_at) AS oldest_pending
FROM email_batches b
JOIN email_jobs j ON j.batch_id = b.id
WHERE b.draw_id = ':active_draw_id'::uuid
  AND b.status = 'sending'
  AND j.status = 'pending'
GROUP BY b.id, b.status
ORDER BY oldest_pending;
```

Do not retry `unknown` jobs until the provider confirms whether delivery occurred.

### Database growth

```sql
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
```

Pool saturation is process-local and appears in `/healthz` under `pool`. Supabase connection utilization is available in the project database reports.

## Deployment sequence

### 1. Prepare the one-shot production cutover

1. Confirm a restorable Supabase backup.
2. Run the complete test suite against disposable local PostgreSQL.
3. Choose and securely record one new draw UUID and one unique migration key.
4. Deploy the application with `RELATIONAL_STORE_ENABLED=false`,
  `TRADING_ENABLED=false`, and `MAINTENANCE_MODE=true`.
5. Verify state-changing administrator routes return 503 while reads and
  authentication remain available.
6. Capture the final Supabase backup.
7. Export the legacy row and retain the complete export directory and checksum
  outside the deployment filesystem.
8. Apply every migration in filename order and verify stored checksums.
9. Verify `anon` and `authenticated` cannot read normalized tables.
10. Run the backfill with the predetermined draw UUID and migration key.
11. Run the same backfill command again and require an idempotent result.
12. Run exact reconciliation and require zero unexplained differences.
13. Verify credential derivation using the existing stable `SECRET_KEY`
  without logging readable codes.
14. Set `ACTIVE_DRAW_ID` to the reconciled draw UUID.

If any step fails, leave maintenance enabled and relational mode disabled.
Preserve the report and repair forward before retrying.

### 2. Enable relational reads and writes

1. Set `RELATIONAL_STORE_ENABLED=true` while maintenance remains enabled.
2. Restart one instance and require successful startup.
3. Require `/healthz` HTTP 200 and the operational command exit 0.
4. Verify public state, admin state, CSV exports, trader login, holdings, import preview, and email preview.
5. Verify one controlled expected-version conflict returns 409.
6. Set `MAINTENANCE_MODE=false`.
7. Keep `TRADING_ENABLED=false` until ownership and draw checks are complete.
8. Enable trading, then verify listing, request, approval, ownership transfer, and transaction-feed behavior with approved test participants.

### 3. Immediate production smoke test

- Public board loads on a separate unauthenticated device after access-code entry.
- Administrator state and ticket totals match reconciliation.
- A zero-ticket participant can authenticate.
- No private table is readable as `anon` or `authenticated`.
- Draw version increments once for one controlled mutation.
- A stale version receives 409 and does not mutate data.
- Marketplace settlement creates one trade, one owner, and one ownership event.
- Notification preview does not resend successful ticket/email pairs.
- `/healthz`, logs, and Supabase reports show no waiters or failures.

## Rollback and incident rules

### Before relational writes begin

If backfill or reconciliation fails, keep relational mode disabled, preserve the failed report, correct the migration code or source ambiguity, and rerun with a clean target draw or the documented idempotent key. Do not edit applied migration checksums.

### After relational writes begin

Do not switch traffic back to mutable legacy JSON. Instead:

1. Set `MAINTENANCE_MODE=true` to stop writes.
2. Keep relational reads available when health permits.
3. Preserve logs, migration ledgers, and affected row IDs.
4. Restore the relational database only from a point-in-time backup when the complete post-backup audit trail can be replayed safely.
5. Reconcile rounds, ownership events, trades, imports, and email outcomes before reopening writes.
6. Rotate credentials only if credential integrity is affected.

A legacy export may be used for forensic comparison or a separately approved full restore. It is not an automatic rollback source after relational mutations.

## Stabilization and legacy retirement

For the agreed stabilization period:

- Keep the final legacy export and database backup immutable.
- Monitor health, latency, pool pressure, locks, 409s, 5xx responses, email outcomes, and table growth daily.
- Perform and document a backup restore drill.
- Re-run reconciliation after any incident investigation.
- Confirm no relational runtime route accesses `draw_state`.

Remove legacy runtime code or the legacy row only after the restore drill succeeds, audit owners approve, and the retention requirement is satisfied.
