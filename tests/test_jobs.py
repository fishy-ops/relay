import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from psycopg_pool import ConnectionPool

from relay import jobs
from relay.jobs import IdempotencyConflict


def enqueue(pool: ConnectionPool, tenant: UUID, key: str, max_attempts: int = 5) -> dict:
    job, created = jobs.create_job(pool, tenant, key, "upscale", {"n": key}, max_attempts)
    assert created
    return job


# --- idempotency -----------------------------------------------------------


def test_same_key_and_body_returns_the_same_job(pool, make_tenant):
    t = make_tenant("acme")
    first, created_first = jobs.create_job(pool, t, "k1", "upscale", {"video": "a"}, 5)
    second, created_second = jobs.create_job(pool, t, "k1", "upscale", {"video": "a"}, 5)
    assert created_first and not created_second
    assert first["id"] == second["id"]


def test_same_key_with_different_body_is_a_conflict(pool, make_tenant):
    t = make_tenant("acme")
    jobs.create_job(pool, t, "k1", "upscale", {"video": "a"}, 5)
    with pytest.raises(IdempotencyConflict):
        jobs.create_job(pool, t, "k1", "upscale", {"video": "b"}, 5)


def test_concurrent_identical_submissions_create_exactly_one_job(pool, make_tenant):
    t = make_tenant("acme")
    workers = 16
    barrier = threading.Barrier(workers)

    def submit(_):
        barrier.wait()  # release all threads at once to maximise the race
        return jobs.create_job(pool, t, "same-key", "upscale", {"video": "a"}, 5)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(submit, range(workers)))

    assert sum(created for _, created in results) == 1
    assert len({job["id"] for job, _ in results}) == 1


def test_keys_are_scoped_per_tenant(pool, make_tenant):
    a, b = make_tenant("a"), make_tenant("b")
    job_a, _ = jobs.create_job(pool, a, "shared", "upscale", {}, 5)
    job_b, _ = jobs.create_job(pool, b, "shared", "upscale", {}, 5)
    assert job_a["id"] != job_b["id"]


def test_get_job_is_tenant_scoped(pool, make_tenant):
    a, b = make_tenant("a"), make_tenant("b")
    job = enqueue(pool, a, "k")
    assert jobs.get_job(pool, a, job["id"]) is not None
    assert jobs.get_job(pool, b, job["id"]) is None


# --- queue and leases ------------------------------------------------------


def test_concurrent_claims_never_hand_out_the_same_job(pool, make_tenant):
    t = make_tenant("acme")
    total = 200
    for i in range(total):
        enqueue(pool, t, f"job-{i}")

    def drain(_):
        claimed = []
        while (job := jobs.claim_next(pool, "w", 60)) is not None:
            claimed.append(job["id"])
        return claimed

    with ThreadPoolExecutor(max_workers=8) as ex:
        batches = list(ex.map(drain, range(8)))

    all_claimed = [jid for batch in batches for jid in batch]
    assert len(all_claimed) == total
    assert len(set(all_claimed)) == total


def test_claimed_job_is_hidden_until_its_lease_expires(pool, make_tenant):
    enqueue(pool, make_tenant("acme"), "k")
    assert jobs.claim_next(pool, "a", 60) is not None
    assert jobs.claim_next(pool, "b", 60) is None


def test_expired_lease_is_reclaimed_with_a_new_fencing_token(pool, make_tenant):
    job = enqueue(pool, make_tenant("acme"), "k")
    first = jobs.claim_next(pool, "a", 0.2)
    time.sleep(0.4)
    second = jobs.claim_next(pool, "b", 60)
    assert second["id"] == job["id"]
    assert second["lease_token"] == first["lease_token"] + 1
    assert second["attempts"] == 2


def test_stale_worker_cannot_write_after_reassignment(pool, make_tenant):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k")
    stale = jobs.claim_next(pool, "frozen", 0.2)
    time.sleep(0.4)
    current = jobs.claim_next(pool, "healthy", 60)

    assert not jobs.heartbeat(pool, job["id"], stale["lease_token"], 60)
    assert not jobs.complete(pool, job["id"], stale["lease_token"], {"by": "frozen"})
    assert jobs.complete(pool, job["id"], current["lease_token"], {"by": "healthy"})

    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "succeeded"
    assert row["output"] == {"by": "healthy"}


def test_expired_but_unclaimed_lease_can_still_complete(pool, make_tenant):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k")
    claim = jobs.claim_next(pool, "slow", 0.2)
    time.sleep(0.4)
    # Nobody reassigned the job, so the token still matches and the result is kept.
    assert jobs.complete(pool, job["id"], claim["lease_token"], {"ok": True})


def test_heartbeat_keeps_the_lease_alive(pool, make_tenant):
    enqueue(pool, make_tenant("acme"), "k")
    claim = jobs.claim_next(pool, "a", 0.5)
    time.sleep(0.3)
    assert jobs.heartbeat(pool, claim["id"], claim["lease_token"], 60)
    time.sleep(0.4)
    assert jobs.claim_next(pool, "b", 60) is None


def test_fail_requeues_until_attempts_run_out(pool, make_tenant):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k", max_attempts=2)

    claim = jobs.claim_next(pool, "a", 60)
    assert jobs.fail(pool, job["id"], claim["lease_token"], "boom")
    assert jobs.get_job(pool, t, job["id"])["status"] == "queued"

    claim = jobs.claim_next(pool, "a", 60)
    assert jobs.fail(pool, job["id"], claim["lease_token"], "boom again")
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == 2


def test_expired_final_attempt_is_failed_and_never_reclaimed(pool, make_tenant):
    t = make_tenant("acme")
    job = enqueue(pool, t, "k", max_attempts=1)
    jobs.claim_next(pool, "crashed", 0.2)
    time.sleep(0.4)

    assert jobs.claim_next(pool, "b", 60) is None
    row = jobs.get_job(pool, t, job["id"])
    assert row["status"] == "failed"
    assert "final attempt" in row["error"]
