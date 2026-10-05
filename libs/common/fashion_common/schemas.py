"""
ProductRecord: the one definition of a clean, ready-to-store product.

Used by:
  - ingestion/prepare.py   -> every record is validated before it is written
  - Catalog Service        -> every webhook product is validated before Postgres

The ORM model (models.Product) says how a product is STORED;
this Pydantic model says what a VALID product looks like.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    parent_asin: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1)
    category: str | None = None
    store: str | None = None
    features: str = ""
    description: str = ""
    image_url: str | None = None

    price: float = Field(gt=0, lt=100_000)
    average_rating: float | None = Field(default=None, ge=0, le=5)
    rating_number: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    avg_review_rating: float | None = Field(default=None, ge=1, le=5)
    bayesian_score: float = Field(ge=0, le=5)

    search_text: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_deleted: bool = False

    @field_validator("parent_asin", "title")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("image_url")
    @classmethod
    def http_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value