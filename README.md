# Relay

An async inference jobs API. Clients submit work and get a job ID back immediately. Workers run the job and record the result under a lease. Relay is the service other apps hand heavy work to.

Week 1 scope: schema, API keys, `POST /v1/jobs` and `GET /v1/jobs/{id}` with idempotency keys, a Postgres lease queue with fencing tokens, and a stub worker.

## Run it

```bash
# Local Postgres (or use docker compose, below)
createdb relay && createdb relay_test

pip install -e ".[dev]"
python -m relay.cli migrate
python -m relay.cli create-tenant "Crisp"   # prints an API key once; store it

uvicorn relay.api:app --reload
python -m relay.worker                       # in another shell
pytest -q
```

With Docker:

```bash
docker compose up --build
```

Compose starts Postgres, runs migrations once, then starts the API on :8000 and one worker.

## Try it

```bash
KEY=rl_...   # from create-tenant

curl -s -X POST localhost:8000/v1/jobs \
  -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"kind": "upscale", "input": {"sleep_seconds": 1}}'

curl -s localhost:8000/v1/jobs/<id> -H "Authorization: Bearer $KEY"
```

Send the same `Idempotency-Key` and body again and you get the same job back with `Idempotent-Replayed: true`. Reusing the key with a different body returns 409.

## Design

- **Postgres is the queue.** Workers claim with `SELECT … FOR UPDATE SKIP LOCKED`. Enqueueing and recording the idempotency key share one transaction. Move to SQS or Kafka when you need much higher throughput, many independent consumers, or replay.
- **Idempotency** is a unique constraint on `(tenant_id, idempotency_key)`. Concurrent duplicates race on the INSERT, and one wins. Each key stores a hash of its body, so a reused key with a different body is rejected.
- **Leases.** A claim sets `lease_expires_at`. If the worker stops heartbeating, the job becomes claimable again.
- **Fencing.** Each claim increments `lease_token`. Heartbeat, complete and fail all require the token they were issued. A frozen worker that wakes after reassignment matches zero rows and its output is discarded.
- **Attempts.** `max_attempts` bounds retries. An expired lease on the final attempt marks the job `failed`.
- **API keys** are random `rl_` tokens. Only their SHA-256 is stored.

Tests cover double-claims under concurrency, concurrent duplicate submissions, stale-worker writes after reassignment, heartbeats, and attempt exhaustion.

## Not yet built

Presigned uploads, the ONNX model, the webhook outbox and dispatcher, the reaper for abandoned jobs, pagination, Redis rate limiting, Terraform and the AWS deployment. See the build plan.
