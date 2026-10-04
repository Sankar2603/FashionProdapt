"""
Query Service (port 8001).

POST /parse: turns what the user typed into a structured search:
  1. cache lookup (Redis)
  2. regex extracts the max price (code owns numbers)
  3. LLM (LangChain structured output) writes an English search phrase
  4. if the LLM fails, times out or returns invalid data -> rule-based fallback
  5. LLM results are cached
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from fashion_common.query_rules import extract_price, rule_parse
from fashion_common.service_app import create_app

from app.cache import IntentCache, cache_key
from app.llm import PROMPT_VERSION, build_llm, run_llm
from app.schemas import ParseRequest, ParseResponse

SERVICE_NAME = "query_service"
logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.llm = build_llm()          # None -> rules only
    app.state.cache = IntentCache.from_env()
    yield
    await app.state.cache.close()


async def health_check(app: FastAPI) -> dict[str, bool]:
    return {"redis": await app.state.cache.ping()}


app = create_app(SERVICE_NAME, lifespan=lifespan, health_check=health_check)


@app.post("/parse", response_model=ParseResponse)
async def parse(body: ParseRequest, request: Request) -> ParseResponse:
    state = request.app.state
    start = time.perf_counter()

    # 1. Cache
    key = cache_key(body.query, PROMPT_VERSION)
    cached = await state.cache.get(key)
    if cached:
        parse_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info("parsed", extra={"source": cached["source"], "cache_hit": True, "parse_ms": parse_ms})
        return ParseResponse(**cached, cache_hit=True, parse_ms=parse_ms)

    # 2. Price: always from the regex, never from the LLM.
    max_price, _ = extract_price(body.query)

    # 3. LLM, with 4. fallback
    result = None
    if state.llm is not None:
        try:
            intent = await run_llm(state.llm, body.query)
            result = {"search_query": intent.search_query.strip(),
                      "language": intent.language.strip().lower(),
                      "source": "llm"}
        except Exception as exc:
            logger.warning("llm failed; using rules", extra={"error": type(exc).__name__,
                                                             "detail": str(exc)[:200]})
    if result is None:
        fallback = rule_parse(body.query)
        result = {"search_query": fallback["search_query"],
                  "language": fallback["language"],
                  "source": "rules"}

    response = {
        "original_query": body.query,
        "search_query": result["search_query"],
        "max_price": max_price,
        "currency": "USD",
        "language": result["language"],
        "source": result["source"],
    }

    # 5. Cache only LLM answers.
    if result["source"] == "llm":
        await state.cache.set(key, response)

    parse_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info("parsed", extra={"source": result["source"], "cache_hit": False,
                                 "max_price": max_price, "parse_ms": parse_ms})
    return ParseResponse(**response, cache_hit=False, parse_ms=parse_ms)