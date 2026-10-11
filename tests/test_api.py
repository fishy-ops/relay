import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.auth import create_tenant


@pytest.fixture
def client(settings, storage):
    with TestClient(create_app(settings, storage=storage)) as c:
        yield c


@pytest.fixture
def acme_key(pool):
    return create_tenant(pool, "acme")


@pytest.fixture
def other_key(pool):
    return create_tenant(pool, "other")


def auth(key: str, idem: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_submit_requires_a_valid_api_key(client, acme_key):
    body = {"kind": "echo", "input": {}}
    assert client.post("/v1/jobs", json=body, headers={"Idempotency-Key": "k"}).status_code == 401
    bad = client.post("/v1/jobs", json=body, headers=auth("rl_nope", "k"))
    assert bad.status_code == 401


def test_revoked_key_is_rejected(client, pool, acme_key):
    with pool.connection() as conn:
        conn.execute("UPDATE api_keys SET revoked_at = now()")
    body = {"kind": "echo", "input": {}}
    assert client.post("/v1/jobs", json=body, headers=auth(acme_key, "k")).status_code == 401


def test_submit_requires_idempotency_key(client, acme_key):
    r = client.post("/v1/jobs", json={"kind": "echo"}, headers=auth(acme_key))
    assert r.status_code == 422


def test_submit_returns_202_and_replays_return_the_same_job(client, acme_key):
    body = {"kind": "echo", "input": {"video": "clip.mp4"}}
    first = client.post("/v1/jobs", json=body, headers=auth(acme_key, "retry-me"))
    assert first.status_code == 202
    assert first.headers["Idempotent-Replayed"] == "false"
    assert first.json()["status"] == "queued"

    again = client.post("/v1/jobs", json=body, headers=auth(acme_key, "retry-me"))
    assert again.status_code == 202
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]


def test_reusing_a_key_with_a_different_body_is_409(client, acme_key):
    headers = auth(acme_key, "k")
    client.post("/v1/jobs", json={"kind": "echo", "input": {"a": 1}}, headers=headers)
    r = client.post("/v1/jobs", json={"kind": "echo", "input": {"a": 2}}, headers=headers)
    assert r.status_code == 409


def test_get_job_returns_it_and_hides_other_tenants_jobs(client, acme_key, other_key):
    created = client.post(
        "/v1/jobs", json={"kind": "echo", "input": {}}, headers=auth(acme_key, "k")
    ).json()

    mine = client.get(f"/v1/jobs/{created['id']}", headers=auth(acme_key))
    assert mine.status_code == 200
    assert mine.json()["id"] == created["id"]

    theirs = client.get(f"/v1/jobs/{created['id']}", headers=auth(other_key))
    assert theirs.status_code == 404


def test_get_unknown_or_malformed_job_id(client, acme_key):
    missing = client.get("/v1/jobs/7f3c2d1e-0000-4000-8000-000000000000", headers=auth(acme_key))
    assert missing.status_code == 404
    assert client.get("/v1/jobs/not-a-uuid", headers=auth(acme_key)).status_code == 422
