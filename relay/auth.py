import hashlib
import secrets
from typing import Any

from psycopg_pool import ConnectionPool

KEY_PREFIX = "rl_"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_tenant(pool: ConnectionPool, name: str, label: str = "default") -> str:
    """Create a tenant and one API key. Returns the plaintext key. It is never stored."""
    key = generate_api_key()
    with pool.connection() as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO api_keys (tenant_id, key_hash, label) VALUES (%s, %s, %s)",
            (tenant_id, hash_api_key(key), label),
        )
    return key


def lookup_tenant(pool: ConnectionPool, key: str) -> dict[str, Any] | None:
    if not key.startswith(KEY_PREFIX):
        return None
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT t.id AS tenant_id, t.name
            FROM api_keys k JOIN tenants t ON t.id = k.tenant_id
            WHERE k.key_hash = %s AND k.revoked_at IS NULL
            """,
            (hash_api_key(key),),
        ).fetchone()
