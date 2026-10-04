"""Request and response models for the Rerank Service."""

from pydantic import BaseModel, Field


class RerankCandidate(BaseModel):
    """One retrieval candidate (same fields the Retrieval Service returns)."""

    parent_asin: str
    title: str
    store: str | None = None
    price: float
    average_rating: float | None = None
    rating_number: int = 0
    bayesian_score: float
    image_url: str | None = None
    search_text: str
    score: float = 0.0                       # retrieval score
    dense_vector: list[float] | None = None  # needed for MMR diversity


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    candidates: list[RerankCandidate] = Field(..., min_length=1, max_length=100)
    top_n: int = Field(5, ge=1, le=50, description="How many results to return")
    mmr_lambda: float = Field(0.7, ge=0.0, le=1.0,
                              description="1.0 = pure relevance, lower = more diversity")
    rating_weight: float = Field(0.1, ge=0.0, le=1.0,
                                 description="How much the Bayesian rating nudges the score")


class RankedProduct(BaseModel):
    parent_asin: str
    title: str
    store: str | None = None
    price: float
    average_rating: float | None = None
    rating_number: int = 0
    bayesian_score: float
    image_url: str | None = None
    relevance: float        # cross-encoder score, 0..1
    final_score: float      # relevance + rating_weight * normalized rating
    retrieval_rank: int     # position in the retrieval results (1 = first)


class Timings(BaseModel):
    score_ms: float
    mmr_ms: float


class RerankResponse(BaseModel):
    query: str
    count: int
    diversity_applied: bool   # False if some candidates had no vectors
    results: list[RankedProduct]
    timings: Timings