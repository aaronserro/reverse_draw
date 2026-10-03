-- Durable email batches, recipient jobs, and ticket-delivery membership.

CREATE TABLE email_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    import_batch_id UUID,
    allocation_fingerprint TEXT NOT NULL
        CHECK (btrim(allocation_fingerprint) <> ''),
    status TEXT NOT NULL
        CHECK (status IN ('sending', 'completed', 'attention', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (id, draw_id),
    FOREIGN KEY (import_batch_id, draw_id)
        REFERENCES import_batches(id, draw_id),
    CHECK (
        (status = 'sending' AND completed_at IS NULL)
        OR (status <> 'sending' AND completed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_sending_email_batch_per_draw
    ON email_batches(draw_id)
    WHERE status = 'sending';

CREATE INDEX email_batches_history_idx
    ON email_batches(draw_id, created_at DESC);

CREATE TABLE email_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    batch_id UUID NOT NULL,
    participant_id UUID,
    recipient_name TEXT NOT NULL CHECK (btrim(recipient_name) <> ''),
    recipient_email CITEXT NOT NULL CHECK (btrim(recipient_email::text) <> ''),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'sent', 'failed', 'unknown', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    provider_request_id TEXT,
    error TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, draw_id),
    UNIQUE (batch_id, recipient_email),
    FOREIGN KEY (batch_id, draw_id)
        REFERENCES email_batches(id, draw_id) ON DELETE CASCADE,
    FOREIGN KEY (participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    CHECK (
        (status = 'sent' AND sent_at IS NOT NULL)
        OR status <> 'sent'
    )
);

CREATE TABLE email_job_tickets (
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    ticket_id UUID NOT NULL,
    is_new BOOLEAN NOT NULL,
    PRIMARY KEY (job_id, ticket_id),
    FOREIGN KEY (job_id, draw_id)
        REFERENCES email_jobs(id, draw_id) ON DELETE CASCADE,
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id)
);

CREATE INDEX email_delivery_lookup_idx
    ON email_jobs(recipient_email, status, created_at DESC);

CREATE INDEX email_job_tickets_ticket_idx
    ON email_job_tickets(ticket_id, job_id);
