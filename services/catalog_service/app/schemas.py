"""Request/response models for the Catalog Service."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# How far in the future an event timestamp may be (sender clock skew).
# A far-future timestamp would block every later update, so it is rejected.
MAX_CLOCK_SKEW = timedelta(minutes=5)


class ProductIn(BaseModel):
    """
    A full snapshot of one product, as the sender knows it right now.

    Text fields may be strings or lists of strings (like the raw Amazon data);
    the service cleans them with the same rules as the batch pipeline.
    """
    model_config = ConfigDict(extra="ignore")

    parent_asin: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1)
    category: str | list[str] | None = None
    store: str | None = None
    features: str | list[str] = ""
    description: str | list[str] = ""
    image_url: str | None = None
    price: float = Field(gt=0)
    average_rating: float | None = Field(default=None, ge=0, le=5)
    rating_number: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    avg_review_rating: float | None = Field(default=None, ge=1, le=5)


class WebhookEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=100,
                          description="Unique per event; retries reuse the same ID")
    event_type: Literal["product.upserted", "product.deleted"]
    occurred_at: datetime = Field(description="When the change happened in the source system")
    parent_asin: str | None = Field(default=None, max_length=20,
                                    description="Required for product.deleted")
    product: ProductIn | None = Field(default=None, description="Required for product.upserted")

    @field_validator("occurred_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:          # no timezone given -> treat as UTC
            value = value.replace(tzinfo=timezone.utc)
        if value > datetime.now(timezone.utc) + MAX_CLOCK_SKEW:
            raise ValueError("occurred_at is in the future")
        return value

    @model_validator(mode="after")
    def check_shape(self) -> "WebhookEvent":
        if self.event_type == "product.upserted":
            if self.product is None:
                raise ValueError("product.upserted needs a 'product'")
            if self.parent_asin and self.parent_asin != self.product.parent_asin:
                raise ValueError("parent_asin does not match product.parent_asin")
            self.parent_asin = self.product.parent_asin
        elif not self.parent_asin:
            raise ValueError("product.deleted needs a 'parent_asin'")
        return self


class WebhookResponse(BaseModel):
    event_id: str
    parent_asin: str
    # applied   -> Postgres changed, sync job queued
    # duplicate -> this event_id was already processed (a retry)
    # stale     -> an equal or newer change was already applied
    # not_found -> delete for a product we don't have
    status: Literal["applied", "duplicate", "stale", "not_found"]
    # created | content_changed | metadata_only | restored | deleted | already_deleted
    change: str | None = None
    queued: bool = False


class ProductOut(BaseModel):
    parent_asin: str
    title: str
    category: str | None
    store: str | None
    features: str
    description: str
    image_url: str | None
    price: float
    average_rating: float | None
    rating_number: int
    review_count: int
    avg_review_rating: float | None
    bayesian_score: float
    content_hash: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    source_updated_at: datetime | None