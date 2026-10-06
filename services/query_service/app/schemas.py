from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ParseRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="What the user typed")

    @field_validator("query")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class LLMIntent(BaseModel):
    """What the LLM must return. Its max_price is only a proposal: code verifies it."""

    search_query: str = Field(
        ...,
        min_length=2,
        max_length=120,
        description=(
            "A short English product search phrase (2 to 10 words) describing what the "
            "user wants: item type, material, style, season, brand, colour. Translate to "
            "English if needed. Do NOT include prices, budgets or currency."
        ),
    )
    language: str = Field(
        ...,
        max_length=20,
        description="ISO 639-1 code of the language the user wrote in, e.g. en, es, hi.",
    )
    max_price: float | None = Field(
        None,
        description=(
            "The shopper's MAXIMUM budget as a number, only if they state an upper limit "
            "(under, below, less than, up to, at most, max, within, or the same idea in any "
            "language). Null if no budget, or if the price is not an upper limit (over, "
            "around, about, from)."
        ),
    )
    currency: str | None = Field(
        None,
        description=(
            "ISO 4217 code of that budget's currency (USD, EUR, GBP, INR...). Null if no "
            "budget. '$' or 'dollars' means USD."
        ),
    )


class ParseResponse(BaseModel):
    original_query: str
    search_query: str          # English phrase for the Retrieval Service
    max_price: float | None    # always USD: regex, or an LLM proposal verified by code
    currency: str = "USD"
    language: str
    source: Literal["llm", "rules"]
    price_source: Literal["regex", "llm"] | None = None
    cache_hit: bool
    parse_ms: float