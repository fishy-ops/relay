from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from relay.auth import create_tenant, lookup_tenant
from relay.config import load_settings  # noqa: I001
from relay.db import make_pool, migrate

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture(scope="session")
def test_url() -> str:
    url = load_settings().test_database_url
    # This fixture drops the public schema. Refuse to run against anything not named *test*.
    assert "test" in url.rsplit("/", 1)[-1], f"refusing to reset non-test database: {url}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    migrate(url, str(MIGRATIONS))
    return url


@pytest.fixture(scope="session")
def pool(test_url: str) -> ConnectionPool:
    p = make_pool(test_url, min_size=1, max_size=20)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def clean_tables(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute("TRUNCATE jobs, api_keys, tenants CASCADE")


@pytest.fixture
def settings(test_url: str):
    return replace(load_settings(), database_url=test_url, admin_token="admin-secret")


@pytest.fixture
def storage():
    import boto3
    from moto import mock_aws

    from relay.storage import Storage

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="relay-test")
        yield Storage("relay-test", client)


@pytest.fixture
def make_tenant(pool: ConnectionPool) -> Callable[[str], UUID]:
    def _make(name: str) -> UUID:
        key = create_tenant(pool, name)
        return lookup_tenant(pool, key)["tenant_id"]

    return _make
