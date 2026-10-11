"""Background process: delivers webhooks, reaps abandoned jobs, publishes queue metrics.

Run one or more of these. Each claim uses SKIP LOCKED, so several dispatchers can
share the outbox without stepping on each other.
"""

import logging
import signal
import time
from typing import Any

import httpx
from psycopg_pool import ConnectionPool

from relay import jobs
from relay.config import Settings, load_settings
from relay.db import make_pool
from relay.telemetry import setup_tracing, tracer
from relay.webhooks import dispatch_due

log = logging.getLogger("relay.dispatcher")

METRICS_INTERVAL_SECONDS = 30


def run_cycle(pool: ConnectionPool, settings: Settings, client: httpx.Client) -> dict[str, int]:
    with tracer().start_as_current_span("dispatcher.cycle"):
        reaped = jobs.reap_expired(pool)
        dispatched = dispatch_due(pool, settings, client)
    return {"reaped": reaped, "dispatched": dispatched}


def publish_metrics(pool: ConnectionPool, cloudwatch: Any) -> None:
    stats = jobs.system_stats(pool)
    cloudwatch.put_metric_data(
        Namespace="Relay",
        MetricData=[
            {"MetricName": "QueueDepth", "Value": stats["jobs"]["queued"], "Unit": "Count"},
            {"MetricName": "Running", "Value": stats["jobs"]["running"], "Unit": "Count"},
            {
                "MetricName": "ThroughputPerMinute",
                "Value": stats["throughput_per_minute"],
                "Unit": "Count",
            },
            {"MetricName": "DeadWebhooks", "Value": stats["webhooks"]["dead"], "Unit": "Count"},
        ],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    setup_tracing("relay-dispatcher", settings.otlp_endpoint)
    pool = make_pool(settings.database_url, min_size=1, max_size=3)
    client = httpx.Client()
    cloudwatch = None
    if settings.publish_metrics:
        import boto3

        cloudwatch = boto3.client("cloudwatch", region_name=settings.aws_region)

    stopping = False

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log.info("dispatcher starting")
    last_metrics = 0.0
    try:
        while not stopping:
            result = run_cycle(pool, settings, client)
            if result["reaped"] or result["dispatched"]:
                log.info("cycle: %s", result)
            if cloudwatch and time.monotonic() - last_metrics >= METRICS_INTERVAL_SECONDS:
                publish_metrics(pool, cloudwatch)
                last_metrics = time.monotonic()
            time.sleep(1.0 if result["dispatched"] == 0 else 0)
    finally:
        client.close()
        pool.close()
        log.info("dispatcher stopped")


if __name__ == "__main__":
    main()
