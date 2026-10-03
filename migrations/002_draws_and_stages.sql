-- Draw identity, persisted schedule, lifecycle, and public version.

CREATE TABLE draws (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    total_tickets INTEGER NOT NULL CHECK (total_tickets > 0),
    completion_label TEXT NOT NULL CHECK (btrim(completion_label) <> ''),
    completion_label_plural TEXT NOT NULL CHECK (btrim(completion_label_plural) <> ''),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'finished', 'archived')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE draw_stages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draw_id UUID NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    label TEXT NOT NULL CHECK (btrim(label) <> ''),
    kind TEXT NOT NULL CHECK (kind IN ('elimination', 'prize')),
    prize TEXT NOT NULL DEFAULT '',
    survivor_target INTEGER NOT NULL CHECK (survivor_target > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draw_id, stage_number),
    UNIQUE (id, draw_id)
);

CREATE INDEX draw_stages_order_idx
    ON draw_stages(draw_id, stage_number);

CREATE TRIGGER draws_set_updated_at
BEFORE UPDATE ON draws
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
