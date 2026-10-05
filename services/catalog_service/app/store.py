"""
Postgres side of the Catalog Service.

apply_event() handles one webhook in ONE transaction:
  1. record the event_id      -> already there? it's a retry: "duplicate"
  2. lock the product row     -> read its current hash / deleted flag
  3. guarded write            -> only if the event is newer than the last one applied
  4. store the outcome on the event row
Because the event_id insert and the product write commit together, a crash
can never leave "event recorded but product not changed" (or the reverse).
"""

import logging
import os
import time
from dataclasses import dataclass

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from fashion_common.catalog import (DESCRIPTION_MAX_CHARS, FEATURES_MAX_CHARS, clean_text,
                                    finalize_product, join_list)
from fashion_common.database import make_engine, make_session_factory
from fashion_common.models import Product, WebhookEvent, ensure_schema
from fashion_common.schemas import ProductRecord

from app.schemas import ProductIn, WebhookEvent as WebhookEventIn

logger = logging.getLogger("catalog_service.store")

CATALOG_MEAN_TTL_S = 600  # recompute the catalog mean rating every 10 minutes


@dataclass
class ApplyResult:
    status: str               # applied | duplicate | stale | not_found
    change: str | None = None


def build_record(product: ProductIn, C: float, m: float) -> ProductRecord:
    """Clean a webhook product exactly like ingestion/prepare.py does, then validate it."""
    if isinstance(product.category, list):
        parts = [clean_text(c) for c in product.category]
        category = ", ".join(p for p in parts if p) or None
    else:
        category = clean_text(product.category) or None

    record = {
        "parent_asin": product.parent_asin.strip(),
        "title": clean_text(product.title),
        "category": category,
        "store": clean_text(product.store) or None,
        "features": join_list(product.features, FEATURES_MAX_CHARS, sep="; "),
        "description": join_list(product.description, DESCRIPTION_MAX_CHARS),
        "image_url": product.image_url or None,
        "price": round(product.price, 2),
        "average_rating": product.average_rating,
        "rating_number": product.rating_number,
        "review_count": product.review_count,
        "avg_review_rating": product.avg_review_rating,
    }
    finalize_product(record, C, m)
    return ProductRecord.model_validate(record)   # raises ValidationError if invalid


def _is_newer(occurred_at):
    """SQL condition: the stored row has no source time yet, or an older one."""
    return or_(Product.source_updated_at.is_(None), Product.source_updated_at < occurred_at)


class CatalogStore:
    def __init__(self, database_url: str | None = None):
        self.engine = make_engine(database_url)
        self.Session = make_session_factory(self.engine)
        self.bayes_m = float(os.getenv("BAYES_M", "20"))
        self._mean_cache: tuple[float, float] | None = None   # (value, computed_at)

    def setup(self) -> None:
        ensure_schema(self.engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- ratings
    def catalog_mean(self) -> float:
        """C for the Bayesian score: mean rating over rated, active products (cached)."""
        now = time.monotonic()
        if self._mean_cache and now - self._mean_cache[1] < CATALOG_MEAN_TTL_S:
            return self._mean_cache[0]
        with self.Session() as session:
            value = session.scalar(
                select(func.avg(Product.average_rating)).where(
                    Product.average_rating.is_not(None),
                    Product.rating_number > 0,
                    Product.is_deleted.is_(False),
                )
            )
        mean = float(value) if value is not None else 0.0
        self._mean_cache = (mean, now)
        return mean

    # -------------------------------------------------------------- reads
    def get_product(self, parent_asin: str) -> Product | None:
        with self.Session() as session:
            return session.get(Product, parent_asin)

    # ------------------------------------------------------------- writes
    def apply_event(self, event: WebhookEventIn, record: ProductRecord | None) -> ApplyResult:
        asin = event.parent_asin
        with self.Session() as session, session.begin():
            # 1. Idempotency: the event_id primary key rejects a second copy.
            inserted = session.execute(
                insert(WebhookEvent)
                .values(event_id=event.event_id, event_type=event.event_type,
                        parent_asin=asin, occurred_at=event.occurred_at, outcome="pending")
                .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
                .returning(WebhookEvent.event_id)
            ).scalar()
            if inserted is None:
                return ApplyResult("duplicate")

            # 2. Lock the current row (if any) so concurrent events for the
            #    same product are applied one at a time.
            current = session.execute(
                select(Product.content_hash, Product.is_deleted)
                .where(Product.parent_asin == asin)
                .with_for_update()
            ).first()

            # 3. Apply.
            if event.event_type == "product.upserted":
                result = self._upsert(session, record, event.occurred_at, current)
            else:
                result = self._delete(session, asin, event.occurred_at, current)

            # 4. Remember what happened to this event.
            session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.event_id == event.event_id)
                .values(outcome=result.status)
            )
        return result

    def _upsert(self, session, record: ProductRecord, occurred_at, current) -> ApplyResult:
        values = {**record.model_dump(), "is_deleted": False, "source_updated_at": occurred_at}
        stmt = insert(Product).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Product.parent_asin],
            set_={**{col: stmt.excluded[col] for col in values if col != "parent_asin"},
                  "updated_at": func.now()},
            where=_is_newer(stmt.excluded.source_updated_at),   # ignore out-of-order events
        ).returning(Product.parent_asin)

        if session.execute(stmt).scalar() is None:
            return ApplyResult("stale")

        if current is None:
            change = "created"
        elif current.is_deleted:
            change = "restored"
        elif current.content_hash != record.content_hash:
            change = "content_changed"     # worker must re-embed
        else:
            change = "metadata_only"       # worker only updates the payload
        return ApplyResult("applied", change)

    def _delete(self, session, asin: str, occurred_at, current) -> ApplyResult:
        if current is None:
            return ApplyResult("not_found")
        updated = session.execute(
            update(Product)
            .where(Product.parent_asin == asin, _is_newer(occurred_at))
            .values(is_deleted=True, source_updated_at=occurred_at, updated_at=func.now())
            .returning(Product.parent_asin)
        ).scalar()
        if updated is None:
            return ApplyResult("stale")
        return ApplyResult("applied", "already_deleted" if current.is_deleted else "deleted")