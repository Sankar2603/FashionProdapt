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
    """What the LLM must return. Kept small on purpose: no prices, no numbers."""

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


class ParseResponse(BaseModel):
    original_query: str
    search_query: str          # English phrase for the Retrieval Service
    max_price: float | None    # from regex only, never from the LLM
    currency: str = "USD"
    language: str
    source: Literal["llm", "rules"]
    cache_hit: bool
    parse_ms: float