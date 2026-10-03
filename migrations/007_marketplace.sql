-- Exact-ticket sale listings, purchase requests, and immutable settlements.

CREATE TABLE listings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL,
    seller_participant_id UUID NOT NULL,
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL
        CHECK (status IN ('open', 'reserved', 'sold', 'cancelled', 'invalidated')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    UNIQUE (id, draw_id),
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id),
    FOREIGN KEY (seller_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    CHECK (
        (status IN ('open', 'reserved') AND closed_at IS NULL)
        OR (status IN ('sold', 'cancelled', 'invalidated') AND closed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_active_listing_per_ticket
    ON listings(ticket_id)
    WHERE status IN ('open', 'reserved');

CREATE INDEX listings_order_book_idx
    ON listings(draw_id, status, price_cents, created_at);

CREATE INDEX listings_seller_idx
    ON listings(draw_id, seller_participant_id, created_at DESC);

CREATE TABLE purchase_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    listing_id UUID NOT NULL,
    buyer_participant_id UUID NOT NULL,
    offered_price_cents BIGINT NOT NULL CHECK (offered_price_cents > 0),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    status TEXT NOT NULL
        CHECK (status IN (
            'pending',
            'approved',
            'declined',
            'withdrawn',
            'expired',
            'superseded'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    UNIQUE (id, draw_id),
    UNIQUE (buyer_participant_id, idempotency_key),
    FOREIGN KEY (listing_id, draw_id)
        REFERENCES listings(id, draw_id),
    FOREIGN KEY (buyer_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    CHECK (
        (status = 'pending' AND decided_at IS NULL)
        OR (status <> 'pending' AND decided_at IS NOT NULL)
    )
);

CREATE INDEX purchase_requests_listing_idx
    ON purchase_requests(listing_id, status, created_at);

CREATE INDEX purchase_requests_buyer_idx
    ON purchase_requests(buyer_participant_id, created_at DESC);

CREATE TABLE trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    listing_id UUID NOT NULL UNIQUE,
    request_id UUID NOT NULL UNIQUE,
    ticket_id UUID NOT NULL,
    seller_participant_id UUID NOT NULL,
    buyer_participant_id UUID NOT NULL,
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL CHECK (status IN ('settled', 'reversed')),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (listing_id, draw_id)
        REFERENCES listings(id, draw_id),
    FOREIGN KEY (request_id, draw_id)
        REFERENCES purchase_requests(id, draw_id),
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id),
    FOREIGN KEY (seller_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    FOREIGN KEY (buyer_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    CHECK (seller_participant_id <> buyer_participant_id)
);

CREATE INDEX trades_feed_idx
    ON trades(draw_id, executed_at DESC);

CREATE TRIGGER listings_set_updated_at
BEFORE UPDATE ON listings
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

ALTER TABLE ticket_ownership_events
ADD CONSTRAINT ownership_event_import_batch_fk
FOREIGN KEY (import_batch_id, draw_id)
REFERENCES import_batches(id, draw_id);

ALTER TABLE ticket_ownership_events
ADD CONSTRAINT ownership_event_trade_fk
FOREIGN KEY (trade_id)
REFERENCES trades(id);
