import io
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from relay import jobs
from relay.api import create_app
from relay.auth import create_tenant, lookup_tenant
from relay.storage import upload_key
from relay.worker import run_once

MODEL = str(Path(__file__).resolve().parent.parent / "models" / "upscale_2x.onnx")


@pytest.fixture
def client(settings, storage):
    with TestClient(create_app(settings, storage=storage)) as c:
        yield c


@pytest.fixture
def acme(client):
    key = create_tenant(client.app.state.pool, "acme")
    return {"Authorization": f"Bearer {key}"}


def idem(headers, key):
    return {**headers, "Idempotency-Key": key}


def tenant_of(client, headers) -> UUID:
    return lookup_tenant(client.app.state.pool, headers["Authorization"][7:])["tenant_id"]


def png_bytes(width=8, height=6) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 90)).save(buf, format="PNG")
    return buf.getvalue()


# --- uploads and upscale ---------------------------------------------------


def test_upload_presign_returns_post_policy_with_size_cap(client, acme):
    r = client.post("/v1/uploads", json={"content_type": "image/png"}, headers=acme)
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("tenants/")
    assert body["fields"]["Content-Type"] == "image/png"
    assert body["max_bytes"] == 25 * 1024 * 1024
    assert "policy" in body["fields"]


def test_upload_rejects_disallowed_content_type(client, acme):
    r = client.post("/v1/uploads", json={"content_type": "text/html"}, headers=acme)
    assert r.status_code == 422


def test_upscale_end_to_end_produces_a_2x_png(client, acme, storage, monkeypatch, settings):
    monkeypatch.setenv("MODEL_PATH", MODEL)
    key = upload_key(tenant_of(client, acme))
    storage.put_bytes(key, png_bytes(8, 6), "image/png")

    r = client.post(
        "/v1/jobs", json={"kind": "upscale", "input_key": key}, headers=idem(acme, "up-1")
    )
    assert r.status_code == 202
    job_id = r.json()["id"]

    assert run_once(client.app.state.pool, "w", settings, storage) is True

    job = client.get(f"/v1/jobs/{job_id}", headers=acme).json()
    assert job["status"] == "succeeded"
    assert job["output"] == {"width": 16, "height": 12}
    assert job["usage_units"] == 1
    assert "Signature=" in job["output_url"] and "Expires=" in job["output_url"]

    row = jobs.get_job(client.app.state.pool, tenant_of(client, acme), UUID(job_id))
    out = Image.open(io.BytesIO(storage.get_bytes(row["output_key"])))
    assert out.size == (16, 12)
    assert out.format == "PNG"


def test_upscale_requires_an_input_key(client, acme):
    r = client.post("/v1/jobs", json={"kind": "upscale"}, headers=idem(acme, "x"))
    assert r.status_code == 400


def test_input_key_must_belong_to_the_submitting_tenant(client, acme, storage):
    other = create_tenant(client.app.state.pool, "other")
    foreign = upload_key(tenant_of(client, {"Authorization": f"Bearer {other}"}))
    storage.put_bytes(foreign, png_bytes(), "image/png")
    r = client.post(
        "/v1/jobs", json={"kind": "upscale", "input_key": foreign}, headers=idem(acme, "steal")
    )
    assert r.status_code == 400
    assert "owned by this tenant" in r.json()["detail"]


def test_missing_input_object_is_rejected_at_submit(client, acme):
    key = upload_key(tenant_of(client, acme))
    r = client.post("/v1/jobs", json={"kind": "upscale", "input_key": key}, headers=idem(acme, "g"))
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_corrupt_input_fails_permanently(client, acme, storage, settings, monkeypatch):
    monkeypatch.setenv("MODEL_PATH", MODEL)
    key = upload_key(tenant_of(client, acme))
    storage.put_bytes(key, b"not an image", "image/png")
    r = client.post(
        "/v1/jobs", json={"kind": "upscale", "input_key": key}, headers=idem(acme, "bad")
    )
    run_once(client.app.state.pool, "w", settings, storage)
    job = client.get(f"/v1/jobs/{r.json()['id']}", headers=acme).json()
    assert job["status"] == "failed"
    assert "not a readable image" in job["error"]
    assert job["attempts"] == 1


# --- pagination and usage --------------------------------------------------


def test_pagination_walks_every_job_exactly_once_newest_first(client, acme):
    created = []
    for i in range(7):
        r = client.post(
            "/v1/jobs", json={"kind": "echo", "input": {"i": i}}, headers=idem(acme, f"p{i}")
        )
        created.append(r.json()["id"])

    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/jobs", params=params, headers=acme).json()
        seen.extend(j["id"] for j in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == created[::-1]


def test_invalid_cursor_is_a_400(client, acme):
    assert client.get("/v1/jobs?cursor=%%%", headers=acme).status_code == 400


def test_usage_counts_units_of_succeeded_jobs(client, acme, pool):
    jobs.create_job(pool, tenant_of(client, acme), "u1", "upscale", {}, 5)
    claim = jobs.claim_next(pool, "w", 60)
    jobs.complete(pool, claim["id"], claim["lease_token"], {"ok": 1}, usage_units=4)
    data = client.get("/v1/usage?days=1", headers=acme).json()["data"]
    assert [(d["jobs"], d["units"]) for d in data] == [(1, 4)]


# --- rate limiting ---------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(settings, storage):
    import redis

    r = redis.Redis.from_url("redis://localhost:6379/15")
    r.flushdb()
    limited = create_app(
        replace(settings, rate_limit_per_minute=3), storage=storage, redis_client=r
    )
    try:
        with TestClient(limited) as c:
            key = create_tenant(c.app.state.pool, "rl")
            headers = {"Authorization": f"Bearer {key}"}
            codes = [c.get("/v1/jobs", headers=headers).status_code for _ in range(4)]
            last = c.get("/v1/jobs", headers=headers)
    finally:
        r.flushdb()
    assert codes == [200, 200, 200, 429]
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1


# --- admin and chaos -------------------------------------------------------


def test_admin_endpoints_need_the_admin_token(client):
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = client.get("/admin/stats", headers={"X-Admin-Token": "admin-secret"})
    assert ok.status_code == 200
    assert set(ok.json()) >= {"jobs", "throughput_per_minute", "re_executions", "webhooks"}


def test_admin_routes_are_hidden_without_an_admin_token(settings, storage):
    with TestClient(create_app(replace(settings, admin_token=None), storage=storage)) as c:
        assert c.get("/admin/stats", headers={"X-Admin-Token": "x"}).status_code == 404


def test_chaos_fire_is_off_unless_enabled(client, acme):
    body = {"tenant_id": str(tenant_of(client, acme)), "count": 5}
    r = client.post("/admin/chaos/fire", json=body, headers={"X-Admin-Token": "admin-secret"})
    assert r.status_code == 404


def test_chaos_fire_enqueues_jobs_when_enabled(settings, storage):
    with TestClient(create_app(replace(settings, chaos_enabled=True), storage=storage)) as c:
        key = create_tenant(c.app.state.pool, "fire")
        tenant = tenant_of(c, {"Authorization": f"Bearer {key}"})
        r = c.post(
            "/admin/chaos/fire",
            json={"tenant_id": str(tenant), "count": 25},
            headers={"X-Admin-Token": "admin-secret"},
        )
        assert r.json() == {"created": 25}
        stats = c.get("/admin/stats", headers={"X-Admin-Token": "admin-secret"}).json()
    assert stats["jobs"]["queued"] == 25
