"""Signed webhooks: URL safety checks, HMAC signing, and the at-least-once dispatcher.

Delivery is at-least-once. A dispatcher that dies after the receiver accepted a
webhook but before the row is marked delivered will resend it. Receivers dedupe on
the Relay-Event-Id header, which stays the same across retries.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import random
import socket
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from psycopg_pool import ConnectionPool

from relay.config import Settings

log = logging.getLogger("relay.webhooks")

SIGNATURE_TOLERANCE_SECONDS = 300
DISPATCH_LEASE_SECONDS = 60


class UnsafeWebhookURL(ValueError):
    pass


def validate_webhook_url(url: str, allow_private: bool) -> None:
    """Reject URLs that could be used to reach internal services (SSRF).

    Checks the scheme, then resolves the host and requires every address to be
    globally routable. The dispatcher runs this check again before each attempt,
    so a DNS change after submission is caught too. A small window remains between
    this check and the connect; closing it fully needs a pinned-IP HTTP client.
    """
    parsed = urlparse(url)
    if parsed.hostname is None:
        raise UnsafeWebhookURL("webhook url needs a host")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURL("webhook url must use http or https")
    if parsed.scheme == "http" and not allow_private:
        raise UnsafeWebhookURL("webhook url must use https")
    if allow_private:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeWebhookURL("webhook host does not resolve") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise UnsafeWebhookURL("webhook host resolves to a non-public address")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Receiver-side check: right key, fresh timestamp, constant-time comparison."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        provided = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, provided)


def retry_delay(attempts: int) -> float:
    """Exponential backoff: 5s, 10s, 20s ... capped at one hour, with jitter."""
    base = min(5 * (2 ** max(attempts - 1, 0)), 3600)
    return base + random.uniform(0, base * 0.2)


def build_event(event_id: UUID, event_type: str, job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "type": event_type,
        "created_at": datetime.now().astimezone().isoformat(),
        "data": {
            "id": str(job["id"]),
            "kind": job["kind"],
            "status": job["status"],
            "output": job["output"],
            "output_key": job["output_key"],
            "error": job["error"],
            "usage_units": job["usage_units"],
        },
    }


def dispatch_due(
    pool: ConnectionPool,
    settings: Settings,
    client: httpx.Client,
    batch_size: int = 20,
) -> int:
    """Send one batch of due webhooks. Returns how many attempts were made."""
    with pool.connection() as conn:
        rows = conn.execute(
            """
            UPDATE webhook_deliveries AS d
            SET lease_until = now() + make_interval(secs => %s)
            WHERE d.id IN (
                SELECT id FROM webhook_deliveries
                WHERE status = 'pending' AND next_attempt_at <= now()
                  AND (lease_until IS NULL OR lease_until < now())
                ORDER BY next_attempt_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            RETURNING d.*,
                (SELECT t.webhook_secret FROM tenants t WHERE t.id = d.tenant_id)
                    AS signing_secret
            """,
            (DISPATCH_LEASE_SECONDS, batch_size),
        ).fetchall()

    for row in rows:
        _attempt(pool, settings, client, row)
    return len(rows)


def _attempt(pool: ConnectionPool, settings: Settings, client: httpx.Client, row: dict) -> None:
    body = json.dumps(row["payload"], separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(time.time())
    attempts = row["attempts"] + 1
    try:
        validate_webhook_url(row["url"], settings.allow_private_webhooks)
        response = client.post(
            row["url"],
            content=body,
            headers={
                "Content-Type": "application/json",
                "Relay-Event-Id": str(row["id"]),
                "Relay-Event-Type": row["event_type"],
                "Relay-Signature": sign(row["signing_secret"], timestamp, body),
            },
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
        )
        ok = 200 <= response.status_code < 300
        error = None if ok else f"HTTP {response.status_code}"
    except (httpx.HTTPError, UnsafeWebhookURL) as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:500]

    if ok:
        _update(pool, row["id"], "delivered", attempts, None, None, delivered=True)
        return
    if attempts >= settings.webhook_max_attempts:
        _update(pool, row["id"], "dead", attempts, None, error)
        log.warning("webhook %s moved to dead letter after %d attempts", row["id"], attempts)
        return
    _update(pool, row["id"], "pending", attempts, retry_delay(attempts), error)


def _update(
    pool: ConnectionPool,
    delivery_id: UUID,
    status: str,
    attempts: int,
    delay: float | None,
    error: str | None,
    delivered: bool = False,
) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE webhook_deliveries
            SET status = %s, attempts = %s, lease_until = NULL, last_error = %s,
                next_attempt_at = now() + make_interval(secs => %s),
                delivered_at = CASE WHEN %s THEN now() ELSE delivered_at END
            WHERE id = %s
            """,
            (status, attempts, error, delay or 0, delivered, delivery_id),
        )
