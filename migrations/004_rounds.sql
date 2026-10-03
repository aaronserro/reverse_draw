-- Audited round executions and per-ticket results.

CREATE TABLE draw_rounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    stage_id UUID NOT NULL,
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    label_snapshot TEXT NOT NULL CHECK (btrim(label_snapshot) <> ''),
    kind_snapshot TEXT NOT NULL
        CHECK (kind_snapshot IN ('elimination', 'prize')),
    prize_snapshot TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL CHECK (btrim(seed) <> ''),
    started_with INTEGER NOT NULL CHECK (started_with > 0),
    survivor_count INTEGER NOT NULL CHECK (
        survivor_count > 0 AND survivor_count < started_with
    ),
    status TEXT NOT NULL CHECK (status IN ('completed', 'undone')),
    executed_at TIMESTAMPTZ NOT NULL,
    undone_at TIMESTAMPTZ,
    undone_by TEXT,
    UNIQUE (id, draw_id),
    FOREIGN KEY (stage_id, draw_id)
        REFERENCES draw_stages(id, draw_id),
    CHECK (
        (status = 'completed' AND undone_at IS NULL)
        OR (status = 'undone' AND undone_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_completed_execution_per_round
    ON draw_rounds(draw_id, round_number)
    WHERE status = 'completed';

CREATE INDEX draw_rounds_history_idx
    ON draw_rounds(draw_id, executed_at, round_number);

CREATE TABLE round_results (
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    round_id UUID NOT NULL,
    ticket_id UUID NOT NULL,
    result TEXT NOT NULL
        CHECK (result IN ('eliminated', 'prize_selected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (round_id, ticket_id),
    FOREIGN KEY (round_id, draw_id)
        REFERENCES draw_rounds(id, draw_id) ON DELETE CASCADE,
    FOREIGN KEY (ticket_id, draw_id)
        REFERENCES tickets(id, draw_id) ON DELETE CASCADE
);

CREATE INDEX round_results_ticket_idx
    ON round_results(ticket_id, round_id);

ALTER TABLE tickets
ADD CONSTRAINT tickets_eliminated_round_fk
FOREIGN KEY (eliminated_round_id, draw_id)
REFERENCES draw_rounds(id, draw_id);
