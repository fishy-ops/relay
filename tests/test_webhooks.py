import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from relay import jobs
from relay.webhooks import (
    UnsafeWebhookURL,
    dispatch_due,
    retry_delay,
    sign,
    validate_webhook_url,
    verify_signature,
)


class Receiver:
    """A local HTTP server that records webhook deliveries and answers with a fixed status."""

    def __init__(self, status: int = 200):
        self.status = status
        self.received: list[dict] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                receiver.received.append({"headers": dict(self.headers), "body": body})
                self.send_response(receiver.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


@pytest.fixture
def open_settings(settings):
    # Local receivers are only reachable when private targets are allowed.
    return replace(settings, allow_private_webhooks=True, webhook_timeout_seconds=2)


def finished_job(pool, tenant, webhook_url, status="succeeded", max_attempts=5):
    job, _ = jobs.create_job(
        pool,
        tenant,
        f"k-{webhook_url}-{status}",
        "echo",
        {},
        max_attempts,
        webhook_url=webhook_url,
    )
    claim = jobs.claim_next(pool, "w", 60)
    if status == "succeeded":
        jobs.complete(pool, claim["id"], claim["lease_token"], {"done": True})
    else:
        jobs.fail(pool, claim["id"], claim["lease_token"], "boom", permanent=True)
    return job


def all_deliveries(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM webhook_deliveries ORDER BY created_at").fetchall()


# --- signing ---------------------------------------------------------------


def test_signature_round_trip_verifies():
    body = b'{"id":"1"}'
    header = sign("s3cret", 1_700_000_000, body)
    assert verify_signature("s3cret", header, body, now=1_700_000_010)


def test_signature_rejects_wrong_secret_tampered_body_and_stale_timestamp():
    body = b'{"id":"1"}'
    header = sign("s3cret", 1_700_000_000, body)
    assert not verify_signature("other", header, body, now=1_700_000_010)
    assert not verify_signature("s3cret", header, b'{"id":"2"}', now=1_700_000_010)
    assert not verify_signature("s3cret", header, body, now=1_700_000_000 + 3600)
    assert not verify_signature("s3cret", "garbage", body, now=1_700_000_010)


# --- SSRF guard ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "file:///etc/passwd",
        "http://example.com/hook",  # plaintext is refused outside dev
    ],
)
def test_unsafe_webhook_urls_are_rejected(url):
    with pytest.raises(UnsafeWebhookURL):
        validate_webhook_url(url, allow_private=False)


def test_private_targets_are_allowed_only_when_explicitly_enabled():
    validate_webhook_url("http://127.0.0.1:9000/hook", allow_private=True)


def test_submit_api_rejects_metadata_endpoint(settings, storage):
    from fastapi.testclient import TestClient

    from relay.api import create_app
    from relay.auth import create_tenant

    with TestClient(create_app(settings, storage=storage)) as client:
        key = create_tenant(client.app.state.pool, "ssrf-probe")
        r = client.post(
            "/v1/jobs",
            json={"kind": "echo", "webhook_url": "https://169.254.169.254/latest"},
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "p1"},
        )
    assert r.status_code == 400
    assert "webhook_url rejected" in r.json()["detail"]


# --- dispatcher ------------------------------------------------------------


def test_delivery_is_signed_and_marked_delivered(pool, make_tenant, open_settings, receiver):
    t = make_tenant("acme")
    finished_job(pool, t, receiver.url)

    with httpx.Client() as client:
        assert dispatch_due(pool, open_settings, client) == 1

    assert len(receiver.received) == 1
    delivery = receiver.received[0]
    with pool.connection() as conn:
        secret = conn.execute("SELECT webhook_secret FROM tenants WHERE id = %s", (t,)).fetchone()[
            "webhook_secret"
        ]
    assert verify_signature(secret, delivery["headers"]["Relay-Signature"], delivery["body"])
    event = json.loads(delivery["body"])
    assert event["type"] == "job.succeeded"
    assert event["data"]["output"] == {"done": True}
    assert delivery["headers"]["Relay-Event-Id"] == event["id"]

    row = all_deliveries(pool)[0]
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None


def test_failed_delivery_backs_off_then_dead_letters(pool, make_tenant, open_settings, receiver):
    receiver.status = 500
    settings = replace(open_settings, webhook_max_attempts=2)
    t = make_tenant("acme")
    finished_job(pool, t, receiver.url, status="failed")

    with httpx.Client() as client:
        dispatch_due(pool, settings, client)
        row = all_deliveries(pool)[0]
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_error"] == "HTTP 500"

        # Backoff keeps it out of the next cycle. Make it due to exercise the second attempt.
        with pool.connection() as conn:
            conn.execute("UPDATE webhook_deliveries SET next_attempt_at = now()")
        dispatch_due(pool, settings, client)

    row = all_deliveries(pool)[0]
    assert row["status"] == "dead"
    assert row["attempts"] == 2
    assert row["event_type"] == "job.failed"


def test_policy_change_dead_letters_unsafe_target_without_sending(pool, make_tenant, settings):
    # Queued while the target was allowed. Dispatch runs under the strict policy.
    t = make_tenant("acme")
    finished_job(pool, t, "https://127.0.0.1/hook")
    strict = replace(settings, webhook_max_attempts=1, allow_private_webhooks=False)
    with httpx.Client() as client:
        dispatch_due(pool, strict, client)
    row = all_deliveries(pool)[0]
    assert row["status"] == "dead"
    assert "non-public address" in row["last_error"]


def test_backoff_grows_and_is_capped():
    assert 5 <= retry_delay(1) <= 6.1
    assert 10 <= retry_delay(2) <= 12.1
    assert retry_delay(50) <= 3600 * 1.2


def test_leased_delivery_is_not_sent_twice(pool, make_tenant, open_settings, receiver):
    t = make_tenant("acme")
    finished_job(pool, t, receiver.url)
    with httpx.Client() as client:
        first = dispatch_due(pool, open_settings, client)
        second = dispatch_due(pool, open_settings, client)
    assert (first, second) == (1, 0)
    assert len(receiver.received) == 1
