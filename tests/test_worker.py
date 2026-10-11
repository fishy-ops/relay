from dataclasses import replace
from pathlib import Path

from relay import jobs
from relay.db import migrate
from relay.handlers import execute
from relay.worker import run_once

MIGRATIONS = str(Path(__file__).resolve().parent.parent / "migrations")


def enqueue(pool, tenant, key, kind, payload, max_attempts=5, webhook_url=None):
    job, _ = jobs.create_job(
        pool, tenant, key, kind, payload, max_attempts, webhook_url=webhook_url
    )
    return job


def test_empty_queue_returns_false(pool, settings, storage):
    assert run_once(pool, "w", settings, storage) is False


def test_successful_job_is_completed_with_output(pool, make_tenant, settings, storage):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k", "echo", {"x": 1})
    assert run_once(pool, "w", settings, storage) is True
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "succeeded"
    assert row["output"] == {"echo": {"x": 1}}


def test_handler_error_requeues_then_fails(pool, make_tenant, settings, storage):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k", "echo", {"fail": True}, max_attempts=2)

    run_once(pool, "w", settings, storage)
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "queued"
    assert "requested failure" in row["error"]

    run_once(pool, "w", settings, storage)
    assert jobs.get_job(pool, t, job["id"])["status"] == "failed"


def test_bad_input_fails_permanently_without_retry(pool, make_tenant, settings, storage):
    t = make_tenant("acme")
    # upscale without an input_key is unusable, so it must not be retried.
    job = enqueue(pool, t, "k", "upscale", {}, max_attempts=5)
    run_once(pool, "w", settings, storage)
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_long_job_keeps_its_lease_through_heartbeats(pool, make_tenant, settings, storage):
    # Lease 0.6s, job runs 1.5s. Without heartbeats another worker would reclaim it.
    t = make_tenant("acme")
    job = enqueue(pool, t, "k", "echo", {"sleep_seconds": 1.5})
    short = replace(settings, lease_seconds=0.6)
    assert run_once(pool, "w", short, storage) is True
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "succeeded"
    assert row["attempts"] == 1


def test_completion_enqueues_webhook_in_the_same_transaction(pool, make_tenant, settings, storage):
    t = make_tenant("acme")
    enqueue(pool, t, "k", "echo", {}, webhook_url="https://example.com/hook")
    run_once(pool, "w", settings, storage)
    with pool.connection() as conn:
        rows = conn.execute("SELECT event_type, status FROM webhook_deliveries").fetchall()
    assert [(r["event_type"], r["status"]) for r in rows] == [("job.succeeded", "pending")]


def test_execute_rejects_unknown_kind(pool, make_tenant, settings, storage):
    from relay.handlers import HandlerContext, JobInputError

    ctx = HandlerContext(tenant_id=make_tenant("acme"), storage=storage)
    try:
        execute({"kind": "nope", "input": {}}, ctx)
    except JobInputError:
        return
    raise AssertionError("expected JobInputError")


def test_migrations_are_idempotent(test_url):
    assert migrate(test_url, MIGRATIONS) == []
