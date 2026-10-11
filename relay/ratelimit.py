"""Per-tenant fixed-window rate limiting in Redis.

Fixed windows are simple and cheap. A burst at a window boundary can reach roughly
twice the limit, which is acceptable for protecting the API. Each tenant gets its own
counter key per minute. Keys expire on their own, so nothing needs cleaning up.
"""

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimiter:
    def __init__(self, redis_client: Any, limit_per_minute: int) -> None:
        self.redis = redis_client
        self.limit = limit_per_minute

    def hit(self, tenant_id: UUID, now: float | None = None) -> Decision:
        current = time.time() if now is None else now
        window = int(current // WINDOW_SECONDS)
        key = f"rl:{tenant_id}:{window}"
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_SECONDS * 2)
        count, _ = pipe.execute()
        retry_after = WINDOW_SECONDS - int(current % WINDOW_SECONDS)
        return Decision(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(self.limit - count, 0),
            retry_after=retry_after,
        )
