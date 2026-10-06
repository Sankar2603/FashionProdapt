"""
Product rules shared by batch ingestion and the live Catalog Service.

Both paths MUST clean text, build the search text and hash it the same way.
If they differed, a product loaded by the batch scripts and then re-sent by a
webhook would get a different content_hash, and the worker would re-embed it
for no reason.

Also holds the names the Catalog Service and the Celery worker agree on
(task name and queue), and the catalogue version counter (bump_catalog_version)
that invalidates the Gateway's search-result cache whenever what search can
see changes.
"""

import hashlib
import html
import os
import re

# Celery contract between the Catalog Service (sender) and the worker (Step 11).
SYNC_TASK_NAME = "worker.sync_product"
SYNC_QUEUE = "catalog_sync"

# Redis key (db 0): a counter that goes up whenever what search can see changes.
CATALOG_VERSION_KEY = "catalog:version"

# Long text slows down embedding and adds little meaning.
FEATURES_MAX_CHARS = 1000
DESCRIPTION_MAX_CHARS = 1000

_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Parsing and cleaning
# ---------------------------------------------------------------------------
def parse_price(value) -> float | None:
    """Prices are usually floats, sometimes strings like '$12.99', often null."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        text = str(value).replace("$", "").replace(",", "").strip()
        try:
            price = float(text)
        except ValueError:
            return None
    return price if price > 0 else None


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
# Ratings
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


# ---------------------------------------------------------------------------
# Search text and hash
# ---------------------------------------------------------------------------
def build_search_text(product: dict) -> str:
    """The text that gets embedded. Only descriptive fields; no price or ratings."""
    lines = [f"Title: {product['title']}"]
    if product.get("category"):
        lines.append(f"Category: {product['category']}")
    if product.get("store"):
        lines.append(f"Brand: {product['store']}")
    if product.get("features"):
        lines.append(f"Features: {product['features']}")
    if product.get("description"):
        lines.append(f"Description: {product['description']}")
    return "\n".join(lines)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def finalize_product(product: dict, C: float, m: float) -> dict:
    """
    Add the derived fields to a cleaned product dict (in place, and returned):
    bayesian_score, search_text, content_hash, is_deleted.

    Expects the descriptive fields to be cleaned already, plus
    average_rating, rating_number, review_count and avg_review_rating.
    """
    product["bayesian_score"] = bayesian_score(
        product.get("average_rating"), product.get("rating_number") or 0, C, m)
    product["search_text"] = build_search_text(product)
    product["content_hash"] = content_hash(product["search_text"])
    product.setdefault("is_deleted", False)
    return product


# ---------------------------------------------------------------------------
# Catalogue version (search-result cache invalidation)
# ---------------------------------------------------------------------------
def bump_catalog_version(client=None) -> bool:
    """Increment the catalogue version so every cached search result becomes stale.
    Pass a redis.Redis client to reuse a connection; otherwise REDIS_URL is used.
    Never raises: if Redis is down, cached results simply expire by their TTL."""
    try:
        if client is None:
            import redis
            client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                          socket_timeout=1, socket_connect_timeout=1)
        client.incr(CATALOG_VERSION_KEY)
        return True
    except Exception:
        return False