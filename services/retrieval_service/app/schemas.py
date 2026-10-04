"""Request and response models for the Retrieval Service."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Search text")
    top_k: int = Field(20, ge=1, le=100, description="How many candidates to return")
    max_price: float | None = Field(None, gt=0, description="Only products at or below this price")
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    include_vectors: bool = Field(False, description="Return each candidate's dense vector (for MMR)")

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class Candidate(BaseModel):
    parent_asin: str
    title: str
    store: str | None = None
    price: float
    average_rating: float | None = None
    rating_number: int = 0
    bayesian_score: float
    image_url: str | None = None
    search_text: str
    score: float  # retrieval score (RRF score in hybrid mode)
    dense_vector: list[float] | None = None  # only when include_vectors=true


class Timings(BaseModel):
    encode_ms: float
    search_ms: float


class RetrieveResponse(BaseModel):
    query: str
    mode: str
    count: int
    candidates: list[Candidate]
    timings: Timings