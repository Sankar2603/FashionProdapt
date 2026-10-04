"""
The LLM step: natural language -> LLMIntent (English search phrase + language).

Uses LangChain's with_structured_output(), so the model must return data that
matches the LLMIntent Pydantic schema; anything else raises and the service
falls back to the rule-based parser.

Settings (env):
    LLM_PROVIDER   groq (default) | ollama
    LLM_MODEL      default openai/gpt-oss-120b
    GROQ_API_KEY   required for groq
    OLLAMA_BASE_URL default http://127.0.0.1:11434
    LLM_TIMEOUT_S  default 8
    LLM_REASONING_EFFORT  optional, for reasoning models on Groq
                          (gpt-oss: low | medium | high). "low" keeps latency down.
"""

import asyncio
import logging
import os

from langchain_core.messages import HumanMessage, SystemMessage

from app.schemas import LLMIntent

logger = logging.getLogger("query_service.llm")

# Bump this whenever the prompt changes: it is part of the cache key, so old
# cached answers from the previous prompt are not reused.
PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You convert online fashion shopping queries into a product search phrase.

Rules:
- search_query: a short ENGLISH phrase (2 to 10 words) naming the product the user wants,
  with useful details: item type, material, style, occasion, season, colour, brand.
- Translate to English if the query is in another language.
- Keep brand and model names exactly as written (e.g. "Nike Air Max", "Levi's 501").
- Never include prices, budgets, currency or numbers about money in search_query.
- language: the ISO 639-1 code of the user's language (en, es, hi, ...)."""


def build_llm():
    """Return a structured-output runnable, or None if no LLM is configured."""
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    model = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    timeout = float(os.getenv("LLM_TIMEOUT_S", "8"))

    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.warning("GROQ_API_KEY not set; using the rule-based parser only")
            return None
        from langchain_groq import ChatGroq
        extra = {}
        effort = os.getenv("LLM_REASONING_EFFORT")
        if effort:
            # Reasoning models "think" before answering; a short rewrite task
            # doesn't need much of that, and less thinking = lower latency.
            extra["reasoning_effort"] = effort
        chat = ChatGroq(model=model, api_key=api_key, temperature=0,
                        timeout=timeout, max_retries=0, **extra)
    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        chat = ChatOllama(model=model, temperature=0,
                          base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider!r} (use groq or ollama)")

    logger.info("llm configured", extra={"provider": provider, "model": model,
                                         "reasoning_effort": os.getenv("LLM_REASONING_EFFORT")})
    return chat.with_structured_output(LLMIntent)


async def run_llm(structured_llm, query: str) -> LLMIntent:
    """Call the LLM with a hard timeout. Raises on any failure."""
    timeout = float(os.getenv("LLM_TIMEOUT_S", "8"))
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=query)]
    result = await asyncio.wait_for(structured_llm.ainvoke(messages), timeout=timeout)
    if not isinstance(result, LLMIntent):
        result = LLMIntent.model_validate(result)
    return result