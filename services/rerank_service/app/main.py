"""
Rerank Service (port 8004).

POST /rerank: re-scores retrieval candidates with a cross-encoder, nudges the
score with the Bayesian rating, then uses MMR to return a top-N that is
relevant but not repetitive.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from fashion_common.errors import ServiceError
from fashion_common.service_app import create_app

from app.ranking import blend, mmr
from app.reranker import Reranker
from app.schemas import RankedProduct, RerankRequest, RerankResponse, Timings

SERVICE_NAME = "rerank_service"
logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start = time.perf_counter()
    app.state.reranker = Reranker()
    logger.info("model loaded", extra={"model": app.state.reranker.model_name,
                                       "device": app.state.reranker.device,
                                       "load_s": round(time.perf_counter() - start, 1)})

    # Warm-up: PyTorch's first inference is much slower than later ones (lazy
    # kernel init, memory allocation). Score one full batch of product-length
    # texts so the shapes real requests use are already warm.
    t0 = time.perf_counter()
    sample = ("Title: Lightweight linen button-down shirt\nBrand: Example\n"
              "Features: 100% linen; breathable; relaxed fit; machine washable; " * 3)
    app.state.reranker.score("linen summer shirt", [sample] * app.state.reranker.batch_size)
    logger.info("model warmed up", extra={"warmup_s": round(time.perf_counter() - t0, 1)})
    yield


def health_check(app: FastAPI) -> dict[str, bool]:
    return {"model_loaded": getattr(app.state, "reranker", None) is not None}


app = create_app(SERVICE_NAME, lifespan=lifespan, health_check=health_check)


@app.post("/rerank", response_model=RerankResponse)
async def rerank(body: RerankRequest, request: Request) -> RerankResponse:
    candidates = body.candidates

    # 1. Cross-encoder relevance (blocking model call -> worker thread).
    t0 = time.perf_counter()
    try:
        relevance = await run_in_threadpool(
            request.app.state.reranker.score, body.query, [c.search_text for c in candidates]
        )
    except Exception as exc:
        logger.exception("reranker failed")
        raise ServiceError(500, "rerank_failed", f"Reranking failed: {type(exc).__name__}") from exc
    score_ms = (time.perf_counter() - t0) * 1000

    # 2. Rating nudge, 3. MMR.
    t1 = time.perf_counter()
    final = blend(relevance, [c.bayesian_score for c in candidates], body.rating_weight)
    has_vectors = all(c.dense_vector for c in candidates)
    vectors = [c.dense_vector for c in candidates] if has_vectors else None
    chosen = mmr(final, vectors, body.top_n, body.mmr_lambda)
    mmr_ms = (time.perf_counter() - t1) * 1000

    results = [
        RankedProduct(
            **candidates[i].model_dump(exclude={"dense_vector", "search_text", "score"}),
            relevance=round(relevance[i], 4),
            final_score=round(final[i], 4),
            retrieval_rank=i + 1,
        )
        for i in chosen
    ]

    logger.info("reranked", extra={"candidates": len(candidates), "returned": len(results),
                                   "diversity_applied": has_vectors,
                                   "score_ms": round(score_ms, 1), "mmr_ms": round(mmr_ms, 1)})
    return RerankResponse(
        query=body.query,
        count=len(results),
        diversity_applied=has_vectors,
        results=results,
        timings=Timings(score_ms=round(score_ms, 1), mmr_ms=round(mmr_ms, 1)),
    )