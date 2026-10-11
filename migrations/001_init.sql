-- Relay schema, version 1.
-- The jobs table doubles as the queue: workers claim rows with
-- SELECT ... FOR UPDATE SKIP LOCKED, so enqueueing and recording the
-- idempotency key happen in one transaction.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Only a SHA-256 of the API key is stored. The plaintext is shown once, at creation.
CREATE TABLE api_keys (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants (id),
    key_hash    text NOT NULL UNIQUE,
    label       text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants (id),
    idempotency_key   text NOT NULL,
    -- Hash of (kind, input). Reusing a key with a different body is a conflict.
    request_hash      text NOT NULL,
    kind              text NOT NULL,
    input             jsonb NOT NULL DEFAULT '{}'::jsonb,
    output            jsonb,
    status            text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts          integer NOT NULL DEFAULT 0,
    max_attempts      integer NOT NULL DEFAULT 5,
    -- Fencing token. Bumped on every claim, so a stale worker's writes can be rejected.
    lease_token       bigint NOT NULL DEFAULT 0,
    lease_owner       text,
    lease_expires_at  timestamptz,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

-- Partial index: only live work is scanned by the claim query.
CREATE INDEX jobs_claimable ON jobs (created_at)
    WHERE status IN ('queued', 'running');

