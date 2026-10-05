"""
Celery tasks.

worker.sync_product  - queued by the Catalog Service for every applied webhook
worker.reconcile     - run by beat on a schedule; finds drift and queues syncs

Both are safe to run any number of times.
"""

import logging
import os
from functools import lru_cache

import httpx
import redis
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import OperationalError

from fashion_common.catalog import SYNC_QUEUE, SYNC_TASK_NAME
from fashion_common.middleware import correlation_id_var

from app.celery_app import BROKER_URL, celery_app
from app.sync import Syncer

logger = logging.getLogger("worker.tasks")

LOCK_TIMEOUT_S = 300   # a lock is released automatically after this, even if the worker dies


class LockBusy(Exception):
    """Another worker is syncing the same product right now."""


# Temporary problems: retry with exponential backoff instead of failing.
RETRYABLE = (OperationalError, ResponseHandlingException, UnexpectedResponse,
             httpx.TransportError, redis.ConnectionError, ConnectionError, TimeoutError,
             LockBusy)


@lru_cache(maxsize=1)
def get_syncer() -> Syncer:
    return Syncer()


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(BROKER_URL)


@celery_app.task(name=SYNC_TASK_NAME, bind=True, autoretry_for=RETRYABLE,
                 retry_backoff=2, retry_backoff_max=60, retry_jitter=True,
                 max_retries=int(os.getenv("SYNC_MAX_RETRIES", "8")))
def sync_product(self, parent_asin: str, event_id: str | None = None,
                 correlation_id: str | None = None) -> dict:
    # Same correlation ID as the webhook request, so one change can be traced
    # from the Catalog Service log to this worker log.
    token = correlation_id_var.set(correlation_id or self.request.id or "-")
    try:
        # One sync per product at a time, even with several workers.
        lock = get_redis().lock(f"lock:sync:{parent_asin}", timeout=LOCK_TIMEOUT_S,
                                blocking_timeout=10)
        if not lock.acquire():
            raise LockBusy(parent_asin)
        try:
            result = get_syncer().sync(parent_asin)
        finally:
            try:
                lock.release()
            except redis.exceptions.LockError:
                pass   # expired already; nothing to release

        logger.info("synced", extra={
            "parent_asin": parent_asin, "action": result.action,
            "collection": result.collection, "lag_ms": result.lag_ms,
            "event_id": event_id, "attempt": self.request.retries + 1,
        })
        return {"parent_asin": parent_asin, "action": result.action, "lag_ms": result.lag_ms}
    finally:
        correlation_id_var.reset(token)


def enqueue_sync(parent_asin: str) -> None:
    sync_product.apply_async(args=[parent_asin], kwargs={"correlation_id": "reconcile"},
                             queue=SYNC_QUEUE)


@celery_app.task(name="worker.reconcile", autoretry_for=RETRYABLE,
                 retry_backoff=5, retry_backoff_max=120, max_retries=3)
def reconcile(since_minutes: int | None = None) -> dict:
    token = correlation_id_var.set("reconcile")
    try:
        return get_syncer().reconcile(since_minutes, enqueue=enqueue_sync)
    finally:
        correlation_id_var.reset(token)