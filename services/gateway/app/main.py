"""
Search Gateway (port 8000): the single public entry point.

POST /search  ->  rate limit  ->  result cache  ->  Query  ->  Retrieval  ->  Rerank  ->  response
The correlation ID (created or read by the shared middleware) is forwarded to
every service, so one ID finds the request in every service's logs.
"""

import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request

from fashion_common.middleware import get_correlation_id
from fashion_common.service_app import create_app

from app.pipeline import SearchPipeline
from app.ratelimit import RateLimiter
from app.result_cache import SearchCache
from app.schemas import Intent, Latency, Product, SearchRequest, SearchResponse

SERVICE_NAME = "gateway"
logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared HTTP client: reuses connections to the services.
    app.state.http = httpx.AsyncClient()
    app.state.pipeline = SearchPipeline(app.state.http)
    redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                        socket_timeout=1, socket_connect_timeout=1)
    app.state.redis = redis_client
    app.state.limiter = RateLimiter(redis_client, int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")))
    # SEARCH_CACHE_TTL_S=0 disables the search-result cache.
    app.state.cache = SearchCache(redis_client, int(os.getenv("SEARCH_CACHE_TTL_S", "600")))
    yield
    await app.state.http.aclose()
    await redis_client.aclose()


app = create_app(SERVICE_NAME, lifespan=lifespan)


@app.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    state = request.app.state
    client_id = request.client.host if request.client else "unknown"
    await state.limiter.check(client_id)

    # The catalogue version is read BEFORE the search runs; a miss is stored
    # under this same version (see result_cache.py for why).
    t0 = time.perf_counter()
    version, cached = await state.cache.lookup(body.query, body.top_n)
    if cached is not None:
        total_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info("search done", extra={"cached": True, "total_ms": total_ms,
                                          "count": cached["count"]})
        return SearchResponse(
            **cached,
            correlation_id=get_correlation_id(),
            query=body.query,
            latency_ms=Latency(query_parse_ms=0, retrieval_ms=0, rerank_ms=0, total_ms=total_ms),
            cached=True,
        )

    result = await state.pipeline.run(body.query, body.top_n)

    response = SearchResponse(
        correlation_id=get_correlation_id(),
        query=body.query,
        intent=Intent(**result["intent"]),
        count=len(result["products"]),
        results=[Product(**p) for p in result["products"]],
        degraded=result["degraded"],
        latency_ms=Latency(**result["latency"]),
    )
    # Don't cache degraded results: the next request should retry the full pipeline.
    if not response.degraded:
        await state.cache.store(version, body.query, body.top_n,
                                response.model_dump(include={"intent", "count", "results", "degraded"}))
    return response
