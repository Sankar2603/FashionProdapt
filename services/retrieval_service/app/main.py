"""
Retrieval Service (port 8003).

POST /retrieve: encodes the query with BGE-M3 and runs a hybrid
(dense + sparse, RRF-fused) search in Qdrant through the active alias.
It is the fast first stage: broad, recall-oriented candidate generation.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from fashion_common.embedder import Embedder
from fashion_common.errors import ServiceError
from fashion_common.service_app import create_app
from fashion_common.vector_store import active_alias, make_client, resolve_alias, search

from app.schemas import Candidate, RetrieveRequest, RetrieveResponse, Timings

SERVICE_NAME = "retrieval_service"
logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the model and connect to Qdrant ONCE, at startup.
    start = time.perf_counter()
    app.state.embedder = Embedder()
    logger.info("model loaded", extra={"model": app.state.embedder.model_name,
                                       "device": app.state.embedder.device,
                                       "load_s": round(time.perf_counter() - start, 1)})

    app.state.qdrant = make_client()
    app.state.alias = active_alias()
    collection = resolve_alias(app.state.qdrant, app.state.alias)
    if collection is None:
        logger.warning("alias not found; searches will fail until it exists",
                       extra={"alias": app.state.alias})
    else:
        logger.info("qdrant ready", extra={"alias": app.state.alias, "collection": collection})

    yield
    app.state.qdrant.close()


def health_check(app: FastAPI) -> dict[str, bool]:
    model_loaded = getattr(app.state, "embedder", None) is not None
    try:
        alias_ok = resolve_alias(app.state.qdrant, app.state.alias) is not None
        qdrant_ok = True
    except Exception:
        qdrant_ok = alias_ok = False
    return {"model_loaded": model_loaded, "qdrant": qdrant_ok, "alias": alias_ok}


app = create_app(SERVICE_NAME, lifespan=lifespan, health_check=health_check)


@app.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(body: RetrieveRequest, request: Request) -> RetrieveResponse:
    state = request.app.state

    # The model and the Qdrant client are blocking calls: run them in a worker
    # thread so the event loop keeps serving other requests.
    t0 = time.perf_counter()
    encoded = (await run_in_threadpool(state.embedder.encode, [body.query]))[0]
    encode_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    try:
        hits = await run_in_threadpool(
            search, state.qdrant, state.alias, encoded, body.top_k, body.max_price, body.mode,
            body.include_vectors,
        )
    except Exception as exc:
        logger.exception("qdrant search failed")
        raise ServiceError(503, "search_backend_unavailable",
                           f"Vector search failed: {type(exc).__name__}") from exc
    search_ms = (time.perf_counter() - t1) * 1000

    candidates = [
        Candidate(
            parent_asin=hit.payload["parent_asin"],
            title=hit.payload["title"],
            store=hit.payload.get("store"),
            price=hit.payload["price"],
            average_rating=hit.payload.get("average_rating"),
            rating_number=hit.payload.get("rating_number") or 0,
            bayesian_score=hit.payload["bayesian_score"],
            image_url=hit.payload.get("image_url"),
            search_text=hit.payload["search_text"],
            score=round(hit.score, 6),
            dense_vector=hit.vector.get("dense") if body.include_vectors and hit.vector else None,
        )
        for hit in hits
    ]

    logger.info("retrieved", extra={"mode": body.mode, "top_k": body.top_k,
                                    "max_price": body.max_price, "count": len(candidates),
                                    "encode_ms": round(encode_ms, 1), "search_ms": round(search_ms, 1)})
    return RetrieveResponse(
        query=body.query,
        mode=body.mode,
        count=len(candidates),
        candidates=candidates,
        timings=Timings(encode_ms=round(encode_ms, 1), search_ms=round(search_ms, 1)),
    )