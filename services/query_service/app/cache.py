import hashlib
import json
import logging
import os

import redis.asyncio as redis

logger = logging.getLogger("query_service.cache")


def normalize(query: str) -> str:
    return " ".join(query.lower().split())


def cache_key(query: str, prompt_version: str) -> str:
    digest = hashlib.md5(normalize(query).encode("utf-8")).hexdigest()
    return f"intent:{prompt_version}:{digest}"


class IntentCache:
    def __init__(self, client: redis.Redis, ttl_seconds: int):
        self.client = client
        self.ttl = ttl_seconds

    @classmethod
    def from_env(cls) -> "IntentCache":
        url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1,
                                      decode_responses=True)
        return cls(client, int(os.getenv("INTENT_CACHE_TTL_S", "86400")))

    async def get(self, key: str) -> dict | None:
        try:
            raw = await self.client.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("cache read failed", extra={"error": type(exc).__name__})
            return None

    async def set(self, key: str, value: dict) -> None:
        try:
            await self.client.set(key, json.dumps(value), ex=self.ttl)
        except Exception as exc:
            logger.warning("cache write failed", extra={"error": type(exc).__name__})

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()