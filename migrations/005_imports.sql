-- Parsed allocation imports. Original files belong in a private Storage bucket.

CREATE TABLE import_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    filename TEXT NOT NULL CHECK (btrim(filename) <> ''),
    storage_path TEXT,
    source_fingerprint TEXT NOT NULL CHECK (btrim(source_fingerprint) <> ''),
    allocation_fingerprint TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('uploaded', 'previewed', 'applied', 'rejected')),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    CHECK (
        (status = 'applied' AND applied_at IS NOT NULL)
        OR status <> 'applied'
    ),
    UNIQUE (id, draw_id)
);

CREATE TABLE import_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_batch_id UUID NOT NULL
        REFERENCES import_batches(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    ticket_number INTEGER CHECK (ticket_number IS NULL OR ticket_number > 0),
    holder_name TEXT,
    normalized_holder_name TEXT,
    email CITEXT,
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_error TEXT,
    UNIQUE (import_batch_id, row_number)
);

CREATE INDEX import_batches_draw_time_idx
    ON import_batches(draw_id, uploaded_at DESC);

CREATE INDEX import_batches_source_fingerprint_idx
    ON import_batches(draw_id, source_fingerprint);

CREATE INDEX import_rows_ticket_idx
    ON import_rows(import_batch_id, ticket_number);

CREATE INDEX import_rows_person_idx
    ON import_rows(import_batch_id, normalized_holder_name);
