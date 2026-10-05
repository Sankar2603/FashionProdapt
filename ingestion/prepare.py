"""
Turns the sampled raw records into clean product records.

For each product:
  - cleans title, features, description (HTML entities, tags, whitespace)
  - parses price, picks one image URL, keeps category/store when present
  - aggregates reviews per parent_asin (review_count, avg_review_rating)
  - computes a Bayesian rating score from the meta file's rating stats
  - builds the search text that will be embedded
  - computes content_hash = sha256(search text)
  - validates the finished record against ProductRecord (Pydantic)

The cleaning, search-text and hash rules live in fashion_common.catalog, so the
live Catalog Service (webhooks) produces exactly the same records.

Run from the project root:
    python -m ingestion.prepare
"""

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from fashion_common.catalog import (DESCRIPTION_MAX_CHARS, FEATURES_MAX_CHARS, clean_text,
                                    finalize_product, join_list, parse_price, pick_image_url,
                                    to_float, to_int)
from fashion_common.schemas import ProductRecord

load_dotenv()

DEFAULT_META = Path("data/dev/meta_dev.jsonl")
DEFAULT_REVIEWS = Path("data/dev/reviews_dev.jsonl")
DEFAULT_OUT = Path("data/processed/products.jsonl")


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------
def aggregate_reviews(path: Path) -> dict[str, dict]:
    """Return {parent_asin: {review_count, avg_review_rating}} from a reviews file."""
    totals: dict[str, list] = {}  # asin -> [count, rating_sum]
    if not path.exists():
        print(f"  Reviews file not found ({path}); review fields will be empty.")
        return {}

    with path.open("r", encoding="utf-8") as src:
        for line in src:
            try:
                review = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(review, dict):
                continue
            asin = review.get("parent_asin")
            rating = to_float(review.get("rating"))
            # Amazon review ratings are 1-5 stars; anything else is bad data.
            if not asin or rating is None or not 1 <= rating <= 5:
                continue
            entry = totals.setdefault(asin, [0, 0.0])
            entry[0] += 1
            entry[1] += rating

    return {
        asin: {"review_count": count, "avg_review_rating": round(total / count, 2)}
        for asin, (count, total) in totals.items()
    }


# ---------------------------------------------------------------------------
# Bayesian prior
# ---------------------------------------------------------------------------
def catalog_mean_rating(products: list[dict]) -> float:
    """C: mean average_rating over products that have ratings."""
    rated = [p["average_rating"] for p in products
             if p["average_rating"] is not None and p["rating_number"] > 0]
    return sum(rated) / len(rated) if rated else 0.0


# ---------------------------------------------------------------------------
# One product
# ---------------------------------------------------------------------------
def clean_product(raw: dict) -> dict | None:
    """Clean one raw meta record. Returns None if it can't be used."""
    asin = raw.get("parent_asin")
    title = clean_text(raw.get("title"))
    price = parse_price(raw.get("price"))
    if not asin or not title or price is None:
        return None

    categories = [clean_text(c) for c in (raw.get("categories") or []) if clean_text(c)]
    return {
        "parent_asin": asin,
        "title": title,
        "category": ", ".join(categories) or None,
        "store": clean_text(raw.get("store")) or None,
        "features": join_list(raw.get("features"), FEATURES_MAX_CHARS, sep="; "),
        "description": join_list(raw.get("description"), DESCRIPTION_MAX_CHARS),
        "price": round(price, 2),
        "average_rating": to_float(raw.get("average_rating")),
        "rating_number": to_int(raw.get("rating_number")),
        "image_url": pick_image_url(raw.get("images")),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Clean sampled products.")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEWS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--bayes-m", type=float, default=float(os.getenv("BAYES_M", "20")),
                        help="Ratings needed before a product's own rating dominates")
    args = parser.parse_args()

    if not args.meta.exists():
        raise SystemExit(f"File not found: {args.meta.resolve()} (run ingestion.sample first)")

    start = time.perf_counter()

    # 1. Clean every product.
    products: list[dict] = []
    skipped = 0
    with args.meta.open("r", encoding="utf-8") as src:
        for line in src:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raw = None
            product = clean_product(raw) if isinstance(raw, dict) else None
            if product is None:
                skipped += 1
                continue
            products.append(product)

    # 2. Reviews.
    reviews = aggregate_reviews(args.reviews)

    # 3. Catalog-wide numbers, then per-product scores, text, hash and validation.
    C = catalog_mean_rating(products)
    m = args.bayes_m

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    rejected = 0
    rejected_fields: Counter = Counter()  # which fields failed validation
    with_reviews = 0
    text_lengths = []
    with args.out.open("w", encoding="utf-8") as dst:
        for p in products:
            r = reviews.get(p["parent_asin"])
            p["review_count"] = r["review_count"] if r else 0
            p["avg_review_rating"] = r["avg_review_rating"] if r else None
            finalize_product(p, C, m)

            try:
                record = ProductRecord.model_validate(p)
            except ValidationError as exc:
                rejected += 1
                for error in exc.errors():
                    rejected_fields[str(error["loc"][0]) if error["loc"] else "?"] += 1
                continue

            with_reviews += 1 if r else 0
            text_lengths.append(len(record.search_text))
            dst.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")
            written += 1

    elapsed = time.perf_counter() - start
    avg_len = sum(text_lengths) / len(text_lengths) if text_lengths else 0
    print(f"Products written:          {written:,}  -> {args.out}")
    print(f"Skipped (unusable):        {skipped:,}")
    print(f"Failed validation:         {rejected:,}"
          + (f"  (fields: {dict(rejected_fields)})" if rejected_fields else ""))
    print(f"Products with reviews:     {with_reviews:,}")
    print(f"Catalog mean rating (C):   {C:.3f}")
    print(f"Bayesian m:                {m:g}")
    print(f"Avg search text length:    {avg_len:.0f} chars")
    print(f"Time:                      {elapsed:.1f}s")


if __name__ == "__main__":
    main()