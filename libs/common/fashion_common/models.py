
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, Numeric, String, Text, func
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

    def __repr__(self) -> str:
        return f"<Product {self.parent_asin} {self.title[:40]!r} ${self.price}>"