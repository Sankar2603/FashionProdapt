"""Public request/response models for the Gateway."""

from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="What the shopper typed")
    top_n: int = Field(5, ge=1, le=20, description="How many products to return")

    @field_validator("query")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class Intent(BaseModel):
    search_query: str          # English phrase used for retrieval and reranking
    max_price: float | None
    language: str
    source: str                # "llm" | "rules" | "gateway_rules"


class Product(BaseModel):
    parent_asin: str
    title: str
    store: str | None = None
    price: float
    average_rating: float | None = None
    rating_number: int = 0
    image_url: str | None = None
    relevance: float | None = None   # cross-encoder score; None if rerank was skipped


class Latency(BaseModel):
    query_parse_ms: float
    retrieval_ms: float
    rerank_ms: float
    total_ms: float


class SearchResponse(BaseModel):
    correlation_id: str
    query: str
    intent: Intent
    count: int
    results: list[Product]
    degraded: list[str]        # which steps fell back, e.g. ["rerank_skipped"]
    latency_ms: Latency