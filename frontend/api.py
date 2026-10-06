"""
Every backend call the frontend makes lives here, so a contract change is a
one-file fix. Pages import these functions and only ever catch ApiError.

The webhook secret is read and used only in this module; pages never see it.
"""

import json
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import redis
from dotenv import load_dotenv

from fashion_common.webhooks import signed_headers

# Running locally: pick up the project-root .env. In Docker the file isn't
# there and compose has already set the environment, so this is a no-op.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
CATALOG_URL = os.getenv("CATALOG_URL", "http://127.0.0.1:8002").rstrip("/")
SERVICE_URLS = {
    "gateway": GATEWAY_URL,
    "query": os.getenv("QUERY_URL", "http://127.0.0.1:8001").rstrip("/"),
    "retrieval": os.getenv("RETRIEVAL_URL", "http://127.0.0.1:8003").rstrip("/"),
    "rerank": os.getenv("RERANK_URL", "http://127.0.0.1:8004").rstrip("/"),
    "catalog": CATALOG_URL,
}
BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
SYNC_QUEUE = "catalog_sync"
BAYES_M = float(os.getenv("BAYES_M", "20"))   # ratings needed to be half-trusted (same as catalog)
TEST_PREFIX = "TEST"                           # demo products; only these get simulated ratings

SEARCH_TIMEOUT_S = 30     # the CPU reranker makes a search take ~8-10 s
CATALOG_TIMEOUT_S = 10
HEALTH_TIMEOUT_S = 3

# Same fields scripts/send_webhook.py copies into a full snapshot.
SNAPSHOT_FIELDS = ("parent_asin", "title", "category", "store", "features", "description",
                   "image_url", "price", "average_rating", "rating_number", "review_count",
                   "avg_review_rating")

# Plain-English meaning of a webhook result, keyed by "change" (or "status").
CHANGE_EXPLANATIONS = {
    "created": "New product stored. The worker embeds it and adds it to search.",
    "content_changed": "Text changed, so the worker re-embeds the product.",
    "metadata_only": "Price or rating change only: the search payload (including the "
                     "Bayesian score) is updated, no re-embedding.",
    "restored": "The product was deleted and is back. The worker re-adds it to search.",
    "deleted": "Removed from search. The row stays in Postgres, marked as deleted.",
    "already_deleted": "It was already deleted; nothing changed in search.",
    "duplicate": "This event ID was processed before (a safe retry); nothing changed.",
    "stale": "A newer change was already applied, so this one was ignored.",
    "not_found": "No product with that ASIN, so there was nothing to delete.",
}

DEGRADED_EXPLANATIONS = {
    "query_service_unavailable": "The LLM query parser was unavailable, so simple rules "
                                 "parsed your query instead.",
    "rerank_skipped": "The reranker was unavailable, so results are in retrieval order "
                      "(no relevance scores).",
}


class ApiError(Exception):
    """One error type for the pages: friendly message plus the backend details."""

    def __init__(self, code: str, message: str, status: int | None = None,
                 correlation_id: str | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.correlation_id = correlation_id
        self.retry_after = retry_after


def _raise_for_error(response: httpx.Response) -> None:
    """Turn the shared {"error": {...}} body into an ApiError."""
    if response.is_success:
        return
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    retry_after = response.headers.get("Retry-After")
    raise ApiError(
        code=error.get("code", "http_error"),
        message=error.get("message", f"HTTP {response.status_code}"),
        status=response.status_code,
        correlation_id=error.get("correlation_id"),
        retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
    )


def _request(method: str, url: str, timeout: float, **kwargs) -> httpx.Response:
    """Send one request; timeouts and connection failures become ApiError."""
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise ApiError("timeout", f"No answer within {timeout:.0f} s.") from None
    except httpx.TransportError:
        raise ApiError("unreachable", f"Could not connect to {url}.") from None


# ------------------------------------------------------------------ search
def search(query: str, top_n: int = 5) -> dict:
    response = _request("POST", f"{GATEWAY_URL}/search", SEARCH_TIMEOUT_S,
                        json={"query": query, "top_n": top_n})
    _raise_for_error(response)
    return response.json()


# ----------------------------------------------------------------- catalog
def get_product(asin: str) -> dict | None:
    """The product as stored in Postgres (deleted ones included); None if unknown."""
    response = _request("GET", f"{CATALOG_URL}/products/{asin}", CATALOG_TIMEOUT_S)
    if response.status_code == 404:
        return None
    _raise_for_error(response)
    return response.json()


def _snapshot(asin: str) -> dict:
    product = get_product(asin)
    if product is None:
        raise ApiError("not_found", f"No product with ASIN {asin}.", status=404)
    return {field: product.get(field) for field in SNAPSHOT_FIELDS}


def _event(event_type: str, product: dict | None = None, parent_asin: str | None = None) -> dict:
    event = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    if product is not None:
        event["product"] = product
    if parent_asin is not None:
        event["parent_asin"] = parent_asin
    return event


def _send_event(event: dict) -> dict:
    """Sign and POST one webhook. The exact signed bytes are the bytes sent."""
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        raise ApiError("config", "WEBHOOK_SECRET is not set for the frontend.")
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json", **signed_headers(secret, body)}
    response = _request("POST", f"{CATALOG_URL}/webhooks/products", CATALOG_TIMEOUT_S,
                        content=body, headers=headers)
    _raise_for_error(response)
    return response.json()


def update_price(asin: str, new_price: float) -> dict:
    product = _snapshot(asin)
    product["price"] = new_price
    return _send_event(_event("product.upserted", product=product))


def edit_product(asin: str, title: str, description: str) -> dict:
    product = _snapshot(asin)
    product["title"] = title
    product["description"] = description
    return _send_event(_event("product.upserted", product=product))


def delete_product(asin: str) -> dict:
    return _send_event(_event("product.deleted", parent_asin=asin))


def restore_product(asin: str) -> dict:
    """Re-send the stored snapshot; the catalog marks the product live again."""
    return _send_event(_event("product.upserted", product=_snapshot(asin)))


def _ratings(average_rating: float | None, rating_number: int,
             review_count: int, avg_review_rating: float | None) -> dict:
    """Rating fields as the catalog expects them: no count means no average."""
    return {
        "average_rating": average_rating if rating_number > 0 else None,
        "rating_number": rating_number,
        "review_count": review_count,
        "avg_review_rating": avg_review_rating if review_count > 0 else None,
    }


def create_product(title: str, price: float, store: str = "", category: str = "",
                   image_url: str = "", description: str = "", features: str = "",
                   average_rating: float | None = None, rating_number: int = 0,
                   review_count: int = 0, avg_review_rating: float | None = None) -> dict:
    """New TEST... product. Ratings are simulated (what a store platform would report)."""
    product = {
        "parent_asin": f"{TEST_PREFIX}{secrets.token_hex(3).upper()}",
        "title": title, "category": category or None, "store": store or None,
        "features": features, "description": description, "image_url": image_url or None,
        "price": price,
        **_ratings(average_rating, rating_number, review_count, avg_review_rating),
    }
    return _send_event(_event("product.upserted", product=product))


def set_ratings(asin: str, average_rating: float | None, rating_number: int,
                review_count: int, avg_review_rating: float | None) -> dict:
    """Simulate new review totals for a product (a metadata_only change)."""
    product = _snapshot(asin)
    product.update(_ratings(average_rating, rating_number, review_count, avg_review_rating))
    return _send_event(_event("product.upserted", product=product))


def explain(result: dict) -> str:
    """Plain-English explanation of a webhook response."""
    key = result.get("change") if result.get("status") == "applied" else result.get("status")
    return CHANGE_EXPLANATIONS.get(key, f"Status: {result.get('status')}.")


# ------------------------------------------------------------------ system
def _health(name: str, url: str) -> dict:
    try:
        response = _request("GET", f"{url}/health", HEALTH_TIMEOUT_S)
        body = response.json()
        return {"service": name, "ok": response.status_code == 200 and body.get("status") == "ok",
                "checks": body.get("checks", {}), "error": None}
    except (ApiError, ValueError) as exc:
        return {"service": name, "ok": False, "checks": {}, "error": str(exc)}


def health_all() -> list[dict]:
    """Health of every service, checked in parallel (one slow service can't stall the page)."""
    with ThreadPoolExecutor(max_workers=len(SERVICE_URLS)) as pool:
        return list(pool.map(lambda item: _health(*item), SERVICE_URLS.items()))


def queue_length() -> int:
    """Jobs waiting in the Celery sync queue (Redis db 1)."""
    try:
        client = redis.Redis.from_url(BROKER_URL, socket_timeout=HEALTH_TIMEOUT_S,
                                      socket_connect_timeout=HEALTH_TIMEOUT_S)
        try:
            return client.llen(SYNC_QUEUE)
        finally:
            client.close()
    except redis.RedisError as exc:
        raise ApiError("unreachable", f"Redis is unreachable: {exc}") from None
