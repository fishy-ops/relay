"""Worker: claims jobs under a lease, runs the handler, records the fenced result.

Long jobs stay leased because a LeaseKeeper thread heartbeats while the handler
runs. If the process freezes or dies, heartbeats stop and the lease expires. Another
worker then reclaims the job, and the old worker's late writes are rejected.
"""

import logging
import os
import random
import signal
import socket
import threading
import time
import uuid
from typing import Any

from psycopg_pool import ConnectionPool

from relay import jobs
from relay.config import Settings, load_settings
from relay.db import make_pool
from relay.handlers import DEFAULT_MODEL_PATH, HandlerContext, JobInputError, execute
from relay.storage import Storage, make_storage
from relay.telemetry import setup_tracing, tracer

log = logging.getLogger("relay.worker")


class LeaseKeeper:
    """Heartbeats a job's lease from a background thread until the context exits."""

    def __init__(self, pool: ConnectionPool, job_id: Any, token: int, lease_seconds: float):
        self.lost = threading.Event()
        self._pool = pool
        self._job_id = job_id
        self._token = token
        self._lease = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)

    def __enter__(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = max(self._lease / 3, 0.05)
        while not self._stop.wait(interval):
            try:
                if not jobs.heartbeat(self._pool, self._job_id, self._token, self._lease):
                    self.lost.set()
                    log.warning("lease for job %s was lost; stopping heartbeats", self._job_id)
                    return
            except Exception:
                log.exception("heartbeat failed for job %s; will retry", self._job_id)


def run_once(
    pool: ConnectionPool,
    owner: str,
    settings: Settings,
    storage: Storage | None = None,
) -> bool:
    """Claim and process one job. Returns False if the queue was empty."""
    job = jobs.claim_next(pool, owner, settings.lease_seconds)
    if job is None:
        return False
    token = job["lease_token"]
    ctx = HandlerContext(
        tenant_id=job["tenant_id"],
        storage=storage,
        model_path=os.environ.get("MODEL_PATH", DEFAULT_MODEL_PATH),
    )

    with tracer().start_as_current_span("job.execute") as span:
        span.set_attribute("relay.job_id", str(job["id"]))
        span.set_attribute("relay.kind", job["kind"])
        span.set_attribute("relay.attempt", job["attempts"])
        with LeaseKeeper(pool, job["id"], token, settings.lease_seconds):
            try:
                result = execute(job, ctx)
            except JobInputError as exc:
                # Bad input will fail the same way on every retry, so fail it now.
                jobs.fail(pool, job["id"], token, str(exc), permanent=True)
                log.info("job %s rejected: %s", job["id"], exc)
                return True
            except Exception as exc:
                recorded = jobs.fail(pool, job["id"], token, f"{type(exc).__name__}: {exc}")
                log.warning("job %s failed (recorded=%s): %s", job["id"], recorded, exc)
                span.record_exception(exc)
                return True

            if settings.chaos_kill_rate and random.random() < settings.chaos_kill_rate:
                # Die after the side effects but before recording the result.
                # This is the realistic duplicate-execution case.
                log.warning("chaos: crashing mid-job %s", job["id"])
                os._exit(137)

            recorded = jobs.complete(
                pool,
                job["id"],
                token,
                result.output,
                output_key=result.output_key,
                usage_units=result.usage_units,
            )
            if not recorded:
                # Reassigned while running. The new owner's result stands.
                log.warning("job %s lost its lease before completing; output discarded", job["id"])
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    setup_tracing("relay-worker", settings.otlp_endpoint)
    owner = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    pool = make_pool(settings.database_url, min_size=1, max_size=3)
    storage = make_storage(settings)

    stopping = False

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s received, finishing current job then exiting", signum)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log.info("worker %s starting (chaos_kill_rate=%s)", owner, settings.chaos_kill_rate)
    try:
        while not stopping:
            try:
                idle = not run_once(pool, owner, settings, storage)
            except Exception:
                # A transient database error should not kill the worker. Back off and retry.
                log.exception("worker loop error; retrying in 1s")
                time.sleep(1.0)
                continue
            if idle:
                time.sleep(settings.worker_poll_seconds)
    finally:
        pool.close()
        log.info("worker %s stopped", owner)


if __name__ == "__main__":
    main()
