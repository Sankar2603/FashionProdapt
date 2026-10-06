"""
Search-result cache in Redis, invalidated by a catalogue version.

Key: search:<version>:<md5 of normalized query>:<top_n>
The version is a counter (CATALOG_VERSION_KEY) that the worker and the alias
scripts bump whenever what search can see changes. A bump makes every older
key unreachable at once; the TTL then cleans them up.

The version is read BEFORE the search runs, and the result is stored under
that same version. So if the catalogue changes while a search is running, the
result (which may reflect the old catalogue) lands under the OLD version and
is never served once the bump is visible: a cached result is never older
than the last catalogue change.

If Redis is down the cache is skipped (fail-open) and a warning is logged:
search staying up matters more than caching.
"""

import hashlib
import json
import logging
import re

import redis.asyncio as redis

from fashion_common.catalog import CATALOG_VERSION_KEY

logger = logging.getLogger("gateway.result_cache")

_SPACES = re.compile(r"\s+")


class SearchCache:
    def __init__(self, client: redis.Redis, ttl_seconds: int):
        self.client = client
        self.ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    @staticmethod
    def _key(version: str, query: str, top_n: int) -> str:
        normalized = _SPACES.sub(" ", query.lower()).strip()
        digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
        return f"search:{version}:{digest}:{top_n}"

    async def lookup(self, query: str, top_n: int) -> tuple[str | None, dict | None]:
        """Return (version, cached payload). The payload is None on a miss;
        both are None if the cache is disabled or Redis is unavailable."""
        if not self.enabled:
            return None, None
        try:
            raw_version = await self.client.get(CATALOG_VERSION_KEY)
            if raw_version is None:
                version = "0"
            elif isinstance(raw_version, bytes):
                version = raw_version.decode()
            else:
                version = str(raw_version)
            raw = await self.client.get(self._key(version, query, top_n))
        except Exception as exc:
            logger.warning("search cache unavailable; skipping it",
                           extra={"error": type(exc).__name__})
            return None, None
        return version, (json.loads(raw) if raw is not None else None)

    async def store(self, version: str | None, query: str, top_n: int, payload: dict) -> None:
        """Cache a result under the version read BEFORE the search ran."""
        if not self.enabled or version is None:
            return
        try:
            await self.client.set(self._key(version, query, top_n),
                                  json.dumps(payload, ensure_ascii=False), ex=self.ttl)
        except Exception as exc:
            logger.warning("search cache store failed", extra={"error": type(exc).__name__})
