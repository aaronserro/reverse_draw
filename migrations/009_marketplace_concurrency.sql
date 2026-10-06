-- Separate market invalidation from draw execution and make pending requests
-- unambiguous under retries from multiple tabs or concurrent clients.

ALTER TABLE draws
ADD COLUMN marketplace_version BIGINT NOT NULL DEFAULT 1
CHECK (marketplace_version > 0);

-- Preserve the oldest pending request for each buyer/listing pair. Any later
-- duplicates predate this constraint and cannot remain actionable.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY listing_id, buyer_participant_id
               ORDER BY created_at, id
           ) AS position
    FROM purchase_requests
    WHERE status = 'pending'
)
UPDATE purchase_requests AS request
SET status = 'superseded', decided_at = now()
FROM ranked
WHERE request.id = ranked.id AND ranked.position > 1;

CREATE UNIQUE INDEX one_pending_request_per_buyer_listing
    ON purchase_requests(listing_id, buyer_participant_id)
    WHERE status = 'pending';

CREATE INDEX purchase_requests_pending_expiry_idx
    ON purchase_requests(draw_id, expires_at)
    WHERE status = 'pending';