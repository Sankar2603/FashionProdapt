"""
Per-client rate limit using Redis (fixed one-minute window).

Each client IP gets a counter key per minute: INCR it, and set it to expire
when the minute ends. Over the limit -> 429 with Retry-After.
Redis is shared, so the limit holds even with several gateway replicas.
If Redis is down the request is allowed (fail-open) and a warning is logged:
search staying up matters more than limiting.
"""

import logging
import time

import redis.asyncio as redis

from fashion_common.errors import ServiceError

logger = logging.getLogger("gateway.ratelimit")


class RateLimiter:
    def __init__(self, client: redis.Redis, limit_per_minute: int):
        self.client = client
        self.limit = limit_per_minute

    async def check(self, client_id: str) -> None:
        if self.limit <= 0:
            return  # disabled
        window = int(time.time() // 60)
        key = f"ratelimit:{client_id}:{window}"
        try:
            count = await self.client.incr(key)
            if count == 1:
                await self.client.expire(key, 60)
        except Exception as exc:
            logger.warning("rate limiter unavailable; allowing request",
                           extra={"error": type(exc).__name__})
            return

        if count > self.limit:
            retry_after = 60 - int(time.time() % 60)
            raise ServiceError(
                429, "rate_limited",
                f"Too many requests: limit is {self.limit} per minute.",
                headers={"Retry-After": str(retry_after)},
            )