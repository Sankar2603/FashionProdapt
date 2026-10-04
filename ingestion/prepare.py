import argparse
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from ingestion.sample import parse_price

load_dotenv()

DEFAULT_META = Path("data/dev/meta_dev.jsonl")
DEFAULT_REVIEWS = Path("data/dev/reviews_dev.jsonl")
DEFAULT_OUT = Path("data/processed/products.jsonl")

# Long descriptions slow down embedding and add little meaning.
FEATURES_MAX_CHARS = 1000
DESCRIPTION_MAX_CHARS = 1000

_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------
def clean_text(value) -> str:
    """Decode HTML entities, drop HTML tags, collapse whitespace."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _TAG.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def join_list(values, max_chars: int, sep: str = " ") -> str:
    """Join a list of strings (features/description) into one cleaned string."""
    if not isinstance(values, list):
        values = [values] if values else []
    parts = [clean_text(v) for v in values]
    text = sep.join(p for p in parts if p)
    return text[:max_chars].rstrip()


def pick_image_url(images) -> str | None:
    """Prefer the MAIN image; fall back to the first image with any URL."""
    if not isinstance(images, list) or not images:
        return None
    main = [img for img in images if isinstance(img, dict) and img.get("variant") == "MAIN"]
    for img in main + [i for i in images if isinstance(i, dict)]:
        for key in ("large", "hi_res", "thumb"):
            if img.get(key):
                return img[key]
    return None


def to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_int(value) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


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
            asin = review.get("parent_asin")
            rating = to_float(review.get("rating"))
            if not asin or rating is None:
                continue
            entry = totals.setdefault(asin, [0, 0.0])
            entry[0] += 1
            entry[1] += rating

    return {
        asin: {"review_count": count, "avg_review_rating": round(total / count, 2)}
        for asin, (count, total) in totals.items()
    }


# ---------------------------------------------------------------------------
# Bayesian rating
# ---------------------------------------------------------------------------
def bayesian_score(R: float | None, v: int, C: float, m: float) -> float:
    """
    score = (v / (v + m)) * R + (m / (v + m)) * C

    R = product's average rating, v = number of ratings,
    C = catalog-wide mean rating, m = how many ratings it takes to be trusted.
    A product with no ratings gets C.
    """
    if R is None or v <= 0:
        return round(C, 4)
    return round((v / (v + m)) * R + (m / (v + m)) * C, 4)


def catalog_mean_rating(products: list[dict]) -> float:
    """C: mean average_rating over products that have ratings."""
    rated = [p["average_rating"] for p in products
             if p["average_rating"] is not None and p["rating_number"] > 0]
    return sum(rated) / len(rated) if rated else 0.0


# ---------------------------------------------------------------------------
# Search text and hash
# ---------------------------------------------------------------------------
def build_search_text(product: dict) -> str:
    """The text that gets embedded. Only descriptive fields; no price or ratings."""
    lines = [f"Title: {product['title']}"]
    if product["category"]:
        lines.append(f"Category: {product['category']}")
    if product["store"]:
        lines.append(f"Brand: {product['store']}")
    if product["features"]:
        lines.append(f"Features: {product['features']}")
    if product["description"]:
        lines.append(f"Description: {product['description']}")
    return "\n".join(lines)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
                product = clean_product(json.loads(line))
            except json.JSONDecodeError:
                product = None
            if product is None:
                skipped += 1
                continue
            products.append(product)

    # 2. Reviews.
    reviews = aggregate_reviews(args.reviews)

    # 3. Catalog-wide numbers, then per-product scores, text and hash.
    C = catalog_mean_rating(products)
    m = args.bayes_m

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with_reviews = 0
    text_lengths = []
    with args.out.open("w", encoding="utf-8") as dst:
        for p in products:
            r = reviews.get(p["parent_asin"])
            p["review_count"] = r["review_count"] if r else 0
            p["avg_review_rating"] = r["avg_review_rating"] if r else None
            with_reviews += 1 if r else 0

            p["bayesian_score"] = bayesian_score(p["average_rating"], p["rating_number"], C, m)
            p["search_text"] = build_search_text(p)
            p["content_hash"] = content_hash(p["search_text"])
            p["is_deleted"] = False
            text_lengths.append(len(p["search_text"]))

            dst.write(json.dumps(p, ensure_ascii=False) + "\n")

    elapsed = time.perf_counter() - start
    avg_len = sum(text_lengths) / len(text_lengths) if text_lengths else 0
    print(f"Products written:          {len(products):,}  -> {args.out}")
    print(f"Skipped (unusable):        {skipped:,}")
    print(f"Products with reviews:     {with_reviews:,}")
    print(f"Catalog mean rating (C):   {C:.3f}")
    print(f"Bayesian m:                {m:g}")
    print(f"Avg search text length:    {avg_len:.0f} chars")
    print(f"Time:                      {elapsed:.1f}s")


if __name__ == "__main__":
    main()