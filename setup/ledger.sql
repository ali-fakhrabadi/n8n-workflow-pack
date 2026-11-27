-- Idempotency ledger shared by the three workflows. Run once in the database the workflows'
-- "Idempotency ledger (PostgreSQL)" credential points at, as an owner role; the workflows'
-- own role then needs only SELECT, INSERT and UPDATE on this one table.
--
-- One row per (workflow, event key). A workflow claims a key with a single atomic
-- INSERT ... ON CONFLICT DO UPDATE ... WHERE status = 'failed' RETURNING: only the first
-- delivery (or a redelivery of a failed one) gets a row back and runs.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    workflow     text        NOT NULL,
    key          text        NOT NULL,
    status       text        NOT NULL
                 CHECK (status IN ('processing', 'done', 'rejected', 'expired', 'skipped', 'failed')),
    attempts     integer     NOT NULL DEFAULT 1,
    execution_id text,
    outcome      jsonb,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow, key),
    CHECK (length(key) BETWEEN 1 AND 300)
);

CREATE INDEX IF NOT EXISTS idempotency_keys_execution ON idempotency_keys (execution_id);

-- Housekeeping (run from a schedule if wanted): finished keys older than the senders' retry
-- window can go, e.g. DELETE FROM idempotency_keys WHERE status <> 'processing'
-- AND updated_at < now() - interval '30 days';
