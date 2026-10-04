"""
The search pipeline: a fixed sequence of calls, NOT an LLM agent.

    Query Service  ->  Retrieval Service  ->  Rerank Service
    (parse)            (top 20 candidates)    (top N, precise + diverse)

Each call has its own timeout. Failures degrade where a sensible fallback
exists, and every fallback is recorded in `degraded`:

    Query fails      -> parse with the shared rules (price regex + raw text)
    Retrieval fails  -> error: without candidates there is nothing to show
    Rerank fails     -> retrieval's own order
"""

import logging
import os
import time

import httpx

from fashion_common.errors import ServiceError
from fashion_common.middleware import CORRELATION_HEADER, get_correlation_id
from fashion_common.query_rules import rule_parse

logger = logging.getLogger("gateway.pipeline")


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


class SearchPipeline:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.query_url = os.getenv("QUERY_SERVICE_URL", "http://127.0.0.1:8001")
        self.retrieval_url = os.getenv("RETRIEVAL_SERVICE_URL", "http://127.0.0.1:8003")
        self.rerank_url = os.getenv("RERANK_SERVICE_URL", "http://127.0.0.1:8004")
        self.query_timeout = float(os.getenv("QUERY_TIMEOUT_S", "10"))
        self.retrieval_timeout = float(os.getenv("RETRIEVAL_TIMEOUT_S", "10"))
        self.rerank_timeout = float(os.getenv("RERANK_TIMEOUT_S", "10"))
        self.retrieve_top_k = int(os.getenv("RETRIEVE_TOP_K", "20"))

    async def _post(self, url: str, payload: dict, timeout: float) -> dict:
        """POST JSON, forwarding the correlation ID. Raises on timeout or non-2xx."""
        response = await self.http.post(
            url, json=payload, timeout=timeout,
            headers={CORRELATION_HEADER: get_correlation_id()},
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------ steps
    async def parse(self, query: str, degraded: list[str]) -> dict:
        try:
            result = await self._post(f"{self.query_url}/parse", {"query": query}, self.query_timeout)
            return {k: result[k] for k in ("search_query", "max_price", "language", "source")}
        except Exception as exc:
            logger.warning("query service failed; using gateway rules",
                           extra={"error": type(exc).__name__})
            degraded.append("query_service_unavailable")
            fallback = rule_parse(query)
            return {**fallback, "source": "gateway_rules"}

    async def retrieve(self, intent: dict) -> list[dict]:
        payload = {"query": intent["search_query"], "top_k": self.retrieve_top_k,
                   "max_price": intent["max_price"], "include_vectors": True}
        try:
            result = await self._post(f"{self.retrieval_url}/retrieve", payload, self.retrieval_timeout)
        except Exception as exc:
            logger.error("retrieval failed", extra={"error": type(exc).__name__})
            raise ServiceError(503, "retrieval_unavailable",
                               "Search is temporarily unavailable. Please try again.") from exc
        return result["candidates"]

    async def rerank(self, query: str, candidates: list[dict], top_n: int,
                     degraded: list[str]) -> list[dict]:
        payload = {"query": query, "candidates": candidates, "top_n": top_n}
        try:
            result = await self._post(f"{self.rerank_url}/rerank", payload, self.rerank_timeout)
            return result["results"]
        except Exception as exc:
            logger.warning("rerank failed; using retrieval order",
                           extra={"error": type(exc).__name__})
            degraded.append("rerank_skipped")
            return [{**c, "relevance": None} for c in candidates[:top_n]]

    # ------------------------------------------------------------------- run
    async def run(self, query: str, top_n: int) -> dict:
        total_start = time.perf_counter()
        degraded: list[str] = []

        t = time.perf_counter()
        intent = await self.parse(query, degraded)
        query_parse_ms = _ms(t)

        t = time.perf_counter()
        candidates = await self.retrieve(intent)
        retrieval_ms = _ms(t)

        t = time.perf_counter()
        if candidates:
            products = await self.rerank(intent["search_query"], candidates, top_n, degraded)
        else:
            products = []  # nothing matched (e.g. the price filter is too strict)
        rerank_ms = _ms(t)

        latency = {"query_parse_ms": query_parse_ms, "retrieval_ms": retrieval_ms,
                   "rerank_ms": rerank_ms, "total_ms": _ms(total_start)}
        logger.info("search done", extra={**latency, "count": len(products),
                                          "degraded": degraded, "source": intent["source"]})
        return {"intent": intent, "products": products, "degraded": degraded, "latency": latency}