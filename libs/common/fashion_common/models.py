"""
Database models (SQLAlchemy ORM).

The Product table is the source of truth for the catalog.
Qdrant is a search index built from it.

WebhookEvent records every webhook the Catalog Service has accepted, so a
retried delivery of the same event is recognised and ignored.
"""

from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Engine, Float, Integer, Numeric, String, Text, func,
                        text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    # Identity
    parent_asin: Mapped[str] = mapped_column(String(20), primary_key=True)

    # Descriptive fields (these feed the search text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    store: Mapped[str | None] = mapped_column(Text)
    features: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[str | None] = mapped_column(Text)

    # Commercial fields (change often; never affect the embedding)
    # Numeric stores money exactly; asdecimal=False returns it as a float.
    price: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=False)
    average_rating: Mapped[float | None] = mapped_column(Float)
    rating_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_review_rating: Mapped[float | None] = mapped_column(Float)
    bayesian_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Search / sync fields
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps (set by Postgres)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # When the SOURCE system made the change we last applied (the webhook's
    # occurred_at). Used to ignore events that arrive out of order.
    # NULL for rows loaded by the batch scripts: any event may update them.
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Product {self.parent_asin} {self.title[:40]!r} ${self.price}>"


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_asin: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # applied | stale | not_found
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)


def ensure_schema(engine: Engine) -> None:
    """
    Create missing tables and add columns introduced after the first load.

    create_all() only creates tables that don't exist; it never adds a column
    to an existing table. The ALTER below is a tiny hand-written migration
    (a real project would use Alembic). Both statements are safe to re-run.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ"
        ))