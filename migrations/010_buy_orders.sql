-- General offers to buy any active ticket and immutable accepted fills.
-- This migration is additive only: existing allocations and marketplace rows
-- are neither altered nor rewritten.

CREATE TABLE buy_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    buyer_participant_id UUID NOT NULL,
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    UNIQUE (id, draw_id),
    UNIQUE (buyer_participant_id, idempotency_key),
    FOREIGN KEY (buyer_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    CHECK (
        (status = 'open' AND closed_at IS NULL)
        OR (status IN ('filled', 'cancelled') AND closed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_open_buy_order_per_buyer
    ON buy_orders(buyer_participant_id)
    WHERE status = 'open';

CREATE INDEX buy_orders_book_idx
    ON buy_orders(draw_id, status, price_cents DESC, created_at, id);

CREATE TABLE buy_order_fills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    buy_order_id UUID NOT NULL UNIQUE,
    ticket_id UUID NOT NULL,
    seller_participant_id UUID NOT NULL,
    buyer_participant_id UUID NOT NULL,
    ownership_event_id UUID NOT NULL UNIQUE,
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (seller_participant_id, idempotency_key),
    FOREIGN KEY (buy_order_id, draw_id)
        REFERENCES buy_orders(id, draw_id),
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id),
    FOREIGN KEY (seller_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    FOREIGN KEY (buyer_participant_id, draw_id)
        REFERENCES draw_participants(id, draw_id),
    FOREIGN KEY (ownership_event_id)
        REFERENCES ticket_ownership_events(id),
    CHECK (seller_participant_id <> buyer_participant_id)
);

CREATE INDEX buy_order_fills_feed_idx
    ON buy_order_fills(draw_id, executed_at DESC, id DESC);

CREATE TRIGGER buy_orders_set_updated_at
BEFORE UPDATE ON buy_orders
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

ALTER TABLE buy_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE buy_order_fills ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE buy_orders FROM anon, authenticated;
REVOKE ALL ON TABLE buy_order_fills FROM anon, authenticated;