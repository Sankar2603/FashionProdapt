"""
Search Gateway (port 8000): the single public entry point.

POST /search  ->  rate limit  ->  Query  ->  Retrieval  ->  Rerank  ->  response
The correlation ID (created or read by the shared middleware) is forwarded to
every service, so one ID finds the request in every service's logs.
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request

from fashion_common.middleware import get_correlation_id
from fashion_common.service_app import create_app

from app.pipeline import SearchPipeline
from app.ratelimit import RateLimiter
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
    yield
    await app.state.http.aclose()
    await redis_client.aclose()


app = create_app(SERVICE_NAME, lifespan=lifespan)


@app.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    client_id = request.client.host if request.client else "unknown"
    await request.app.state.limiter.check(client_id)

    result = await request.app.state.pipeline.run(body.query, body.top_n)

    return SearchResponse(
        correlation_id=get_correlation_id(),
        query=body.query,
        intent=Intent(**result["intent"]),
        count=len(result["products"]),
        results=[Product(**p) for p in result["products"]],
        degraded=result["degraded"],
        latency_ms=Latency(**result["latency"]),
    )