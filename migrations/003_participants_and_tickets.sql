-- Stable participants, credentials, canonical ticket ownership, and ownership audit.

CREATE TABLE draw_participants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL CHECK (btrim(display_name) <> ''),
    normalized_name TEXT NOT NULL CHECK (btrim(normalized_name) <> ''),
    source_email CITEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draw_id, normalized_name),
    UNIQUE (id, draw_id)
);

CREATE TABLE holder_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id UUID NOT NULL
        REFERENCES draw_participants(id) ON DELETE CASCADE,
    credential_external_id TEXT NOT NULL UNIQUE
        CHECK (btrim(credential_external_id) <> ''),
    code_digest TEXT NOT NULL CHECK (btrim(code_digest) <> ''),
    code_scheme TEXT NOT NULL CHECK (btrim(code_scheme) <> ''),
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at TIMESTAMPTZ,
    UNIQUE (participant_id)
);

CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_number INTEGER NOT NULL CHECK (ticket_number > 0),
    owner_participant_id UUID,
    eliminated_round_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draw_id, ticket_number),
    UNIQUE (id, draw_id),
    FOREIGN KEY (owner_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id)
);

CREATE TABLE ticket_ownership_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL,
    from_participant_id UUID,
    to_participant_id UUID,
    reason TEXT NOT NULL
        CHECK (reason IN (
            'initial_import',
            'admin_assignment',
            'admin_unassignment',
            'admin_correction',
            'trade'
        )),
    import_batch_id UUID,
    trade_id UUID,
    actor_type TEXT NOT NULL CHECK (btrim(actor_type) <> ''),
    actor_identifier TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_participant_id IS DISTINCT FROM to_participant_id),
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id) ON DELETE CASCADE,
    FOREIGN KEY (from_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    FOREIGN KEY (to_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id)
);

CREATE INDEX participants_name_lookup_idx
    ON draw_participants(draw_id, normalized_name);

CREATE INDEX tickets_owner_idx
    ON tickets(draw_id, owner_participant_id);

CREATE INDEX ownership_events_ticket_time_idx
    ON ticket_ownership_events(ticket_id, created_at DESC);

CREATE INDEX ownership_events_draw_time_idx
    ON ticket_ownership_events(draw_id, created_at DESC);

CREATE TRIGGER draw_participants_set_updated_at
BEFORE UPDATE ON draw_participants
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER tickets_set_updated_at
BEFORE UPDATE ON tickets
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
