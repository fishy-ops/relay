import base64
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from relay import jobs
from relay.auth import lookup_tenant
from relay.config import Settings, load_settings
from relay.db import make_pool
from relay.ratelimit import RateLimiter
from relay.storage import Storage, key_belongs_to, make_storage, upload_key
from relay.telemetry import setup_tracing
from relay.webhooks import UnsafeWebhookURL, validate_webhook_url

STATIC_DIR = Path(__file__).parent / "static"
DOWNLOAD_EXPIRES_SECONDS = 900


class UploadCreate(BaseModel):
    content_type: Literal["image/png", "image/jpeg", "image/webp"]


class UploadOut(BaseModel):
    key: str
    url: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int


class JobCreate(BaseModel):
    kind: Literal["echo", "upscale"]
    input: dict[str, Any] = Field(default_factory=dict)
    input_key: str | None = Field(default=None, max_length=512)
    webhook_url: str | None = Field(default=None, max_length=2048)


class JobOut(BaseModel):
    id: UUID
    kind: str
    status: str
    attempts: int
    output: dict[str, Any] | None
    error: str | None
    input_key: str | None
    output_url: str | None
    usage_units: int
    created_at: datetime
    updated_at: datetime


class JobPage(BaseModel):
    data: list[JobOut]
    next_cursor: str | None


class UsageDay(BaseModel):
    day: date
    jobs: int
    units: int


class UsageOut(BaseModel):
    days: int
    data: list[UsageDay]


class ChaosFire(BaseModel):
    tenant_id: UUID
    count: int = Field(ge=1, le=10_000)
    sleep_seconds: float = Field(default=0, ge=0, le=5)


def _encode_cursor(row: dict[str, Any]) -> str:
    raw = f"{row['created_at'].isoformat()}|{row['id']}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created, job_id = raw.split("|", 1)
        return datetime.fromisoformat(created), UUID(job_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    redis_client: Any | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    storage = storage if storage is not None else make_storage(settings)
    setup_tracing("relay-api", settings.otlp_endpoint)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = make_pool(settings.database_url)
        client = redis_client
        if client is None and settings.redis_url:
            import redis

            client = redis.Redis.from_url(settings.redis_url)
        app.state.limiter = (
            RateLimiter(client, settings.rate_limit_per_minute) if client is not None else None
        )
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(title="Relay", version="0.2.0", lifespan=lifespan)
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # instrumentation is optional; the API must still start
        pass

    def current_tenant(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        scheme, _, key = (authorization or "").partition(" ")
        tenant = lookup_tenant(request.app.state.pool, key) if scheme.lower() == "bearer" else None
        if tenant is None:
            raise HTTPException(401, "invalid or missing API key", {"WWW-Authenticate": "Bearer"})
        limiter: RateLimiter | None = request.app.state.limiter
        if limiter is not None:
            decision = limiter.hit(tenant["tenant_id"])
            if not decision.allowed:
                raise HTTPException(
                    429,
                    "rate limit exceeded",
                    {
                        "Retry-After": str(decision.retry_after),
                        "X-RateLimit-Limit": str(decision.limit),
                    },
                )
        return tenant

    def admin_only(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
        # Without ADMIN_TOKEN configured, the admin routes do not exist.
        if settings.admin_token is None:
            raise HTTPException(404, "not found")
        if x_admin_token is None or not secrets.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(401, "invalid admin token")

    def job_out(row: dict[str, Any]) -> JobOut:
        output_url = None
        if row["status"] == "succeeded" and row["output_key"]:
            output_url = storage.presign_download(row["output_key"], DOWNLOAD_EXPIRES_SECONDS)
        return JobOut(**row, output_url=output_url)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.post("/v1/uploads", status_code=201, response_model=UploadOut)
    def create_upload(
        body: UploadCreate, tenant: dict[str, Any] = Depends(current_tenant)
    ) -> UploadOut:
        """Return a presigned POST. The client uploads straight to storage, not through Relay."""
        key = upload_key(tenant["tenant_id"])
        presigned = storage.presign_upload(key, body.content_type, settings.presign_expires_seconds)
        return UploadOut(
            key=key,
            url=presigned["url"],
            fields=presigned["fields"],
            expires_in=settings.presign_expires_seconds,
            max_bytes=25 * 1024 * 1024,
        )

    @app.post("/v1/jobs", status_code=202, response_model=JobOut)
    def submit_job(
        body: JobCreate,
        request: Request,
        response: Response,
        tenant: dict[str, Any] = Depends(current_tenant),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ) -> JobOut:
        """Accept a job and return at once. A retry with the same key returns the same job."""
        tenant_id = tenant["tenant_id"]
        if body.kind == "upscale" and not body.input_key:
            raise HTTPException(400, "upscale jobs need an input_key from /v1/uploads")
        if body.input_key:
            if not key_belongs_to(body.input_key, tenant_id):
                raise HTTPException(400, "input_key must be an upload owned by this tenant")
            if not storage.exists(body.input_key):
                raise HTTPException(400, "input_key not found; upload the file first")
        if body.webhook_url:
            try:
                validate_webhook_url(body.webhook_url, settings.allow_private_webhooks)
            except UnsafeWebhookURL as exc:
                raise HTTPException(400, f"webhook_url rejected: {exc}") from exc

        try:
            row, created = jobs.create_job(
                request.app.state.pool,
                tenant_id,
                idempotency_key,
                body.kind,
                body.input,
                settings.max_attempts,
                input_key=body.input_key,
                webhook_url=body.webhook_url,
            )
        except jobs.IdempotencyConflict:
            raise HTTPException(
                409, "Idempotency-Key was already used with a different body"
            ) from None
        response.headers["Idempotent-Replayed"] = "false" if created else "true"
        return job_out(row)

    @app.get("/v1/jobs", response_model=JobPage)
    def list_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = None,
        tenant: dict[str, Any] = Depends(current_tenant),
    ) -> JobPage:
        after = _decode_cursor(cursor) if cursor else None
        rows = jobs.list_jobs(request.app.state.pool, tenant["tenant_id"], limit + 1, after)
        page = rows[:limit]
        next_cursor = _encode_cursor(page[-1]) if len(rows) > limit else None
        return JobPage(data=[job_out(r) for r in page], next_cursor=next_cursor)

    @app.get("/v1/jobs/{job_id}", response_model=JobOut)
    def read_job(
        job_id: UUID,
        request: Request,
        tenant: dict[str, Any] = Depends(current_tenant),
    ) -> JobOut:
        # Another tenant's job gets the same 404 as a job that does not exist.
        row = jobs.get_job(request.app.state.pool, tenant["tenant_id"], job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        return job_out(row)

    @app.get("/v1/usage", response_model=UsageOut)
    def usage(
        request: Request,
        days: int = Query(default=30, ge=1, le=366),
        tenant: dict[str, Any] = Depends(current_tenant),
    ) -> UsageOut:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = jobs.usage_by_day(request.app.state.pool, tenant["tenant_id"], since)
        return UsageOut(days=days, data=[UsageDay(**r) for r in rows])

    @app.get("/admin/stats", dependencies=[Depends(admin_only)])
    def admin_stats(request: Request) -> dict[str, Any]:
        return jobs.system_stats(request.app.state.pool)

    @app.post("/admin/chaos/fire", dependencies=[Depends(admin_only)])
    def chaos_fire(body: ChaosFire, request: Request) -> dict[str, int]:
        """Enqueue a batch of echo jobs for load and chaos runs. Off unless RELAY_CHAOS_ENABLED."""
        if not settings.chaos_enabled:
            raise HTTPException(404, "not found")
        pool = request.app.state.pool
        for _ in range(body.count):
            jobs.create_job(
                pool,
                body.tenant_id,
                f"chaos-{uuid4().hex}",
                "echo",
                {"sleep_seconds": body.sleep_seconds},
                settings.max_attempts,
            )
        return {"created": body.count}

    return app


app = create_app()
