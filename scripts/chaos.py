"""Chaos harness: real worker processes, killed mid-job, then checked for correctness.

It enqueues N echo jobs, runs W worker processes, and SIGKILLs a random worker every
--kill-every seconds. Each worker also crashes itself with probability --kill-rate
after doing a job's side effects but before recording the result. Crashed workers
are replaced. When the queue drains, it verifies that:

  - every job reached 'succeeded'           (lost jobs must be 0)
  - no job is stuck in 'running'            (leases were reclaimed)
  - re-executions are counted, not hidden   (at-least-once: a crash after side
                                             effects reruns the job)

    python scripts/chaos.py --jobs 10000 --workers 4 --kill-every 2 --kill-rate 0.002

Exit code 0 means no job was lost. Re-executions are reported separately, because
at-least-once execution cannot promise zero re-runs once workers crash mid-job.
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay import jobs  # noqa: E402
from relay.auth import create_tenant, lookup_tenant  # noqa: E402
from relay.config import load_settings  # noqa: E402
from relay.db import make_pool  # noqa: E402


def spawn_worker(database_url: str, lease: float, kill_rate: float) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "LEASE_SECONDS": str(lease),
        "CHAOS_KILL_RATE": str(kill_rate),
        "WORKER_POLL_SECONDS": "0.05",
        "MODEL_PATH": str(ROOT / "models" / "upscale_2x.onnx"),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "relay.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def enqueue(pool: ConnectionPool, tenant_id, count: int, sleep_seconds: float) -> None:
    payload = {"sleep_seconds": sleep_seconds}
    digest = jobs.request_hash("echo", payload)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO jobs (tenant_id, idempotency_key, request_hash, kind, input, max_attempts)
            VALUES (%s, %s, %s, 'echo', %s, 5)
            """,
            [
                (tenant_id, f"chaos-{uuid.uuid4().hex}", digest, Jsonb(payload))
                for _ in range(count)
            ],
        )


def summarize(pool: ConnectionPool, tenant_id) -> dict:
    with pool.connection() as conn:
        statuses = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, count(*) AS n FROM jobs WHERE tenant_id = %s GROUP BY status",
                (tenant_id,),
            )
        }
        execs = conn.execute(
            """
            SELECT coalesce(sum(n - 1), 0)::int AS extra, count(*) FILTER (WHERE n > 1) AS multi
            FROM (SELECT j.id, count(e.id) AS n
                  FROM jobs j LEFT JOIN job_executions e ON e.job_id = j.id
                  WHERE j.tenant_id = %s GROUP BY j.id) x
            """,
            (tenant_id,),
        ).fetchone()
        stuck = conn.execute(
            """
            SELECT count(*) AS n FROM jobs
            WHERE tenant_id = %s AND status = 'running' AND lease_expires_at < now()
            """,
            (tenant_id,),
        ).fetchone()["n"]
    return {
        "statuses": statuses,
        "re_executions": execs["extra"],
        "jobs_executed_more_than_once": execs["multi"],
        "stuck_expired_leases": stuck,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--lease", type=float, default=2.0)
    parser.add_argument("--sleep", type=float, default=0.02, help="simulated work per job")
    parser.add_argument("--kill-every", type=float, default=2.0, help="seconds between SIGKILLs")
    parser.add_argument("--kill-rate", type=float, default=0.002, help="in-job crash probability")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings()
    pool = make_pool(settings.database_url, min_size=1, max_size=4)
    key = create_tenant(pool, f"chaos-{int(time.time())}")
    tenant_id = lookup_tenant(pool, key)["tenant_id"]

    print(f"enqueueing {args.jobs} jobs for tenant {tenant_id}", flush=True)
    enqueue(pool, tenant_id, args.jobs, args.sleep)

    workers = [
        spawn_worker(settings.database_url, args.lease, args.kill_rate) for _ in range(args.workers)
    ]
    sigkills = in_job_crashes = 0
    started = time.monotonic()
    next_kill = started + args.kill_every
    try:
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            for i, proc in enumerate(workers):
                if proc.poll() is not None:
                    in_job_crashes += 1
                    workers[i] = spawn_worker(settings.database_url, args.lease, args.kill_rate)
            if now >= next_kill:
                victim = random.choice(workers)
                if victim.poll() is None:
                    victim.send_signal(signal.SIGKILL)
                    sigkills += 1
                next_kill = now + args.kill_every
            with pool.connection() as conn:
                pending = conn.execute(
                    "SELECT count(*) AS n FROM jobs WHERE tenant_id = %s"
                    " AND status IN ('queued', 'running')",
                    (tenant_id,),
                ).fetchone()["n"]
            if pending == 0:
                break
            if now - started > args.timeout:
                print("timeout reached with jobs still pending", file=sys.stderr)
                break
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in workers:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    elapsed = time.monotonic() - started
    summary = summarize(pool, tenant_id)
    succeeded = summary["statuses"].get("succeeded", 0)
    report = {
        "jobs": args.jobs,
        "workers": args.workers,
        "seconds": round(elapsed, 2),
        "throughput_per_second": round(succeeded / elapsed, 1) if elapsed else None,
        "sigkills": sigkills,
        "in_job_crashes": in_job_crashes,
        "lost": args.jobs - succeeded,
        **summary,
    }
    pool.close()
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.report:
        args.report.write_text(text)
    ok = report["lost"] == 0 and summary["stuck_expired_leases"] == 0
    print("PASS: no jobs lost" if ok else "FAIL: jobs lost or stuck", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
