import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    test_database_url: str
    lease_seconds: float
    max_attempts: int
    worker_poll_seconds: float
    migrations_dir: str
    # Object storage (S3, or MinIO when S3_ENDPOINT_URL is set)
    s3_bucket: str
    s3_endpoint_url: str | None
    aws_region: str
    presign_expires_seconds: int
    # Redis-backed rate limiting. Disabled when REDIS_URL is unset.
    redis_url: str | None
    rate_limit_per_minute: int
    # Webhooks
    webhook_max_attempts: int
    allow_private_webhooks: bool
    webhook_timeout_seconds: float
    # Ops
    admin_token: str | None
    chaos_enabled: bool
    chaos_kill_rate: float
    otlp_endpoint: str | None
    publish_metrics: bool


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://relay:relay@localhost:5432/relay"
        ),
        test_database_url=os.environ.get(
            "TEST_DATABASE_URL", "postgresql://relay:relay@localhost:5432/relay_test"
        ),
        lease_seconds=float(os.environ.get("LEASE_SECONDS", "30")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "5")),
        worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "0.5")),
        migrations_dir=os.environ.get("MIGRATIONS_DIR", "migrations"),
        s3_bucket=os.environ.get("S3_BUCKET", "relay-dev"),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        presign_expires_seconds=int(os.environ.get("PRESIGN_EXPIRES_SECONDS", "900")),
        redis_url=os.environ.get("REDIS_URL") or None,
        rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "600")),
        webhook_max_attempts=int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "8")),
        allow_private_webhooks=_bool("ALLOW_PRIVATE_WEBHOOKS", False),
        webhook_timeout_seconds=float(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "5")),
        admin_token=os.environ.get("ADMIN_TOKEN") or None,
        chaos_enabled=_bool("RELAY_CHAOS_ENABLED", False),
        chaos_kill_rate=float(os.environ.get("CHAOS_KILL_RATE", "0")),
        otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
        publish_metrics=_bool("CLOUDWATCH_METRICS", False),
    )
