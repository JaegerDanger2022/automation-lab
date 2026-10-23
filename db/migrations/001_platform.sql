-- Shared platform: every build logs its runs here so demos can show a live timeline
-- and so failures, costs and limits are visible in one place.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    build         text NOT NULL,                      -- closedesk | speedtolead | supportdesk | fleetpulse
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'succeeded', 'failed', 'held')),
    is_demo       boolean NOT NULL DEFAULT false,
    input_ref     text,                               -- file name, email, message id
    visitor_hash  text,                               -- hashed IP or phone, never the raw value
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    cost_usd      numeric(10, 5) NOT NULL DEFAULT 0,
    error         text,
    meta          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS runs_build_started_idx ON runs (build, started_at DESC);

CREATE TABLE IF NOT EXISTS run_steps (
    id           bigserial PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name         text NOT NULL,
    status       text NOT NULL CHECK (status IN ('started', 'ok', 'warn', 'failed', 'skipped')),
    at           timestamptz NOT NULL DEFAULT now(),
    duration_ms  integer,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS run_steps_run_idx ON run_steps (run_id, id);

-- Stops the same email, file or message being processed twice.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         text PRIMARY KEY,
    build       text NOT NULL,
    run_id      uuid REFERENCES runs(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Daily counters for rate limits: per visitor per demo, and per demo overall.
CREATE TABLE IF NOT EXISTS usage_counters (
    scope   text NOT NULL,
    day     date NOT NULL DEFAULT current_date,
    count   integer NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);

-- Spend tracking per provider per day, read by the kill switch.
CREATE TABLE IF NOT EXISTS spend_ledger (
    day       date NOT NULL DEFAULT current_date,
    provider  text NOT NULL,
    usd       numeric(10, 5) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

CREATE TABLE IF NOT EXISTS settings (
    key    text PRIMARY KEY,
    value  jsonb NOT NULL
);

INSERT INTO settings (key, value) VALUES
    ('demos_paused', 'false'::jsonb),
    ('monthly_spend_cap_usd', '10'::jsonb),
    ('limits', '{"per_visitor_per_demo_per_day": 5, "per_demo_per_day": 200}'::jsonb)
ON CONFLICT (key) DO NOTHING;
