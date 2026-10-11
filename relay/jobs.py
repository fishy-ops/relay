"""Job repository: idempotent creation, lease queue, transactional outbox, usage.

Every write a worker makes is fenced by lease_token. A claim bumps the token, so a
worker that froze and woke after its lease was reassigned matches zero rows.

Terminal writes (succeeded, failed) insert their webhook row in the same
transaction. A job cannot finish without its notification being recorded.
"""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from relay.webhooks import build_event


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was reused with a different request body."""


def request_hash(
    kind: str,
    input: dict[str, Any],
    input_key: str | None = None,
    webhook_url: str | None = None,
) -> str:
    canonical = json.dumps(
        {"kind": kind, "input": input, "input_key": input_key, "webhook_url": webhook_url},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_job(
    pool: ConnectionPool,
    tenant_id: UUID,
    idempotency_key: str,
    kind: str,
    input: dict[str, Any],
    max_attempts: int,
    input_key: str | None = None,
    webhook_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert a job, or return the existing one for this (tenant, key).

    Returns (job, created). Concurrent calls with the same key race on the INSERT.
    The unique constraint lets one win. The others wait for it to commit, then read
    the winner's row.
    """
    digest = request_hash(kind, input, input_key, webhook_url)
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO jobs (tenant_id, idempotency_key, request_hash, kind, input,
                              max_attempts, input_key, webhook_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING *
            """,
            (
                tenant_id,
                idempotency_key,
                digest,
                kind,
                Jsonb(input),
                max_attempts,
                input_key,
                webhook_url,
            ),
        ).fetchone()
        if row is not None:
            return row, True
        existing = conn.execute(
            "SELECT * FROM jobs WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant_id, idempotency_key),
        ).fetchone()
    if existing["request_hash"] != digest:
        raise IdempotencyConflict(idempotency_key)
    return existing, False


def get_job(pool: ConnectionPool, tenant_id: UUID, job_id: UUID) -> dict[str, Any] | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE id = %s AND tenant_id = %s", (job_id, tenant_id)
        ).fetchone()


def list_jobs(
    pool: ConnectionPool,
    tenant_id: UUID,
    limit: int,
    after: tuple[datetime, UUID] | None = None,
) -> list[dict[str, Any]]:
    """Newest first. `after` is the (created_at, id) of the last row on the previous page."""
    with pool.connection() as conn:
        if after is None:
            return conn.execute(
                """
                SELECT * FROM jobs WHERE tenant_id = %s
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()
        return conn.execute(
            """
            SELECT * FROM jobs
            WHERE tenant_id = %s AND (created_at, id) < (%s, %s)
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (tenant_id, after[0], after[1], limit),
        ).fetchall()


def _enqueue_webhook(conn: psycopg.Connection, job: dict[str, Any], event_type: str) -> None:
    if not job["webhook_url"]:
        return
    delivery_id = uuid4()
    conn.execute(
        """
        INSERT INTO webhook_deliveries (id, job_id, tenant_id, url, event_type, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            delivery_id,
            job["id"],
            job["tenant_id"],
            job["webhook_url"],
            event_type,
            Jsonb(build_event(delivery_id, event_type, job)),
        ),
    )


# Expired leases that already used every attempt cannot be retried. Fail them first.
_FAIL_EXHAUSTED_SQL = """
UPDATE jobs
SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL,
    error = 'lease expired after final attempt', updated_at = now()
WHERE status = 'running' AND lease_expires_at < now() AND attempts >= max_attempts
RETURNING *
"""

# SKIP LOCKED: two claimers never block on, or receive, the same row.
_CLAIM_SQL = """
WITH next AS (
    SELECT id FROM jobs
    WHERE attempts < max_attempts
      AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs AS j
SET status = 'running',
    attempts = j.attempts + 1,
    lease_token = j.lease_token + 1,
    lease_owner = %(owner)s,
    lease_expires_at = now() + make_interval(secs => %(lease)s),
    updated_at = now()
FROM next
WHERE j.id = next.id
RETURNING j.*
"""


def reap_expired(pool: ConnectionPool) -> int:
    """Mark exhausted, abandoned jobs failed and notify their webhooks. Returns the count."""
    with pool.connection() as conn:
        return _reap(conn)


def _reap(conn: psycopg.Connection) -> int:
    failed = conn.execute(_FAIL_EXHAUSTED_SQL).fetchall()
    for job in failed:
        _enqueue_webhook(conn, job, "job.failed")
    return len(failed)


def claim_next(pool: ConnectionPool, owner: str, lease_seconds: float) -> dict[str, Any] | None:
    """Claim the oldest runnable job. Also records the execution, in the same transaction."""
    with pool.connection() as conn:
        _reap(conn)
        job = conn.execute(_CLAIM_SQL, {"owner": owner, "lease": lease_seconds}).fetchone()
        if job is not None:
            conn.execute(
                "INSERT INTO job_executions (job_id, lease_token, owner) VALUES (%s, %s, %s)",
                (job["id"], job["lease_token"], owner),
            )
        return job


def heartbeat(pool: ConnectionPool, job_id: UUID, lease_token: int, lease_seconds: float) -> bool:
    """Extend the lease. False means this worker no longer holds it and should stop."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
            WHERE id = %s AND lease_token = %s AND status = 'running'
            RETURNING id
            """,
            (lease_seconds, job_id, lease_token),
        ).fetchone()
    return row is not None


def complete(
    pool: ConnectionPool,
    job_id: UUID,
    lease_token: int,
    output: dict[str, Any],
    output_key: str | None = None,
    usage_units: int = 0,
) -> bool:
    """Record success and its webhook, atomically. A stale worker gets False.

    An expired lease is still accepted if no other worker has claimed the job,
    because the token only changes on reassignment.
    """
    with pool.connection() as conn:
        job = conn.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', output = %s, output_key = %s, usage_units = %s,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id = %s AND lease_token = %s AND status = 'running'
            RETURNING *
            """,
            (Jsonb(output), output_key, usage_units, job_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        _enqueue_webhook(conn, job, "job.succeeded")
    return True


def fail(
    pool: ConnectionPool,
    job_id: UUID,
    lease_token: int,
    error: str,
    permanent: bool = False,
) -> bool:
    """Requeue the job, or mark it failed (and notify) once attempts run out.

    permanent=True fails immediately. Use it for errors a retry cannot fix.
    """
    with pool.connection() as conn:
        job = conn.execute(
            """
            UPDATE jobs
            SET status = CASE WHEN %s OR attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                error = %s, lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id = %s AND lease_token = %s AND status = 'running'
            RETURNING *
            """,
            (permanent, error[:2000], job_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        if job["status"] == "failed":
            _enqueue_webhook(conn, job, "job.failed")
    return True


def usage_by_day(pool: ConnectionPool, tenant_id: UUID, since: datetime) -> list[dict[str, Any]]:
    """Billable units for succeeded jobs, grouped by UTC day."""
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT (updated_at AT TIME ZONE 'UTC')::date AS day,
                   count(*) AS jobs,
                   coalesce(sum(usage_units), 0) AS units
            FROM jobs
            WHERE tenant_id = %s AND status = 'succeeded' AND updated_at >= %s
            GROUP BY 1 ORDER BY 1
            """,
            (tenant_id, since),
        ).fetchall()


def system_stats(pool: ConnectionPool) -> dict[str, Any]:
    """Operator view across all tenants. Feeds the dashboard and the chaos harness."""
    with pool.connection() as conn:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")
        }
        recent = conn.execute(
            """
            SELECT count(*) AS n FROM jobs
            WHERE status = 'succeeded' AND updated_at > now() - interval '60 seconds'
            """
        ).fetchone()["n"]
        expired = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE status = 'running' AND lease_expires_at < now()"
        ).fetchone()["n"]
        executions = conn.execute(
            """
            SELECT coalesce(sum(n - 1), 0)::int AS extra, count(*) FILTER (WHERE n > 1) AS jobs
            FROM (SELECT job_id, count(*) AS n FROM job_executions GROUP BY job_id) x
            """
        ).fetchone()
        webhooks = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, count(*) AS n FROM webhook_deliveries GROUP BY status"
            )
        }
    return {
        "jobs": {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "total": sum(counts.values()),
        },
        "throughput_per_minute": recent,
        "expired_leases": expired,
        "re_executions": executions["extra"],
        "jobs_executed_more_than_once": executions["jobs"],
        "webhooks": {
            "pending": webhooks.get("pending", 0),
            "delivered": webhooks.get("delivered", 0),
            "dead": webhooks.get("dead", 0),
        },
    }
