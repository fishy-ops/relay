-- Relay schema, version 2: object storage keys, webhook outbox, execution log, pagination index.

ALTER TABLE tenants
    ADD COLUMN webhook_secret text NOT NULL DEFAULT encode(gen_random_bytes(32), 'hex');

ALTER TABLE jobs
    ADD COLUMN input_key   text,
    ADD COLUMN output_key  text,
    ADD COLUMN webhook_url text,
    ADD COLUMN usage_units integer NOT NULL DEFAULT 0;

-- Keyset pagination for GET /v1/jobs, newest first.
CREATE INDEX jobs_tenant_recent ON jobs (tenant_id, created_at DESC, id DESC);

-- Transactional outbox. Rows are inserted in the same statement that marks a job
-- terminal, so a job can never finish without its webhook being recorded.
CREATE TABLE webhook_deliveries (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid NOT NULL REFERENCES jobs (id),
    tenant_id        uuid NOT NULL REFERENCES tenants (id),
    url              text NOT NULL,
    event_type       text NOT NULL,
    payload          jsonb NOT NULL,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'delivered', 'dead')),
    attempts         integer NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    lease_until      timestamptz,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    delivered_at     timestamptz
);

CREATE INDEX webhook_deliveries_due ON webhook_deliveries (next_attempt_at)
    WHERE status = 'pending';

-- One row per execution attempt. Used to measure duplicate executions under chaos.
CREATE TABLE job_executions (
    id          bigserial PRIMARY KEY,
    job_id      uuid NOT NULL REFERENCES jobs (id),
    lease_token bigint NOT NULL,
    owner       text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX job_executions_job ON job_executions (job_id);
