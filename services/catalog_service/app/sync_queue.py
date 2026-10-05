"""
Publishes "sync this product" jobs to the Redis queue for the Celery worker.

The job carries only the parent_asin (plus IDs for tracing). The worker reads
the CURRENT row from Postgres when it runs, so jobs are idempotent and their
order doesn't matter: running a job twice, or an older job after a newer one,
still ends with Qdrant matching Postgres.

The task is sent BY NAME, so this service doesn't import the worker's code.
"""

import logging

import redis
from celery import Celery

from fashion_common.catalog import SYNC_QUEUE, SYNC_TASK_NAME

logger = logging.getLogger("catalog_service.queue")


class SyncQueue:
    def __init__(self, broker_url: str):
        self.broker_url = broker_url
        self.celery = Celery("catalog_service", broker=broker_url)
        # Fail fast instead of hanging if Redis is down.
        self.celery.conf.broker_connection_timeout = 2
        self.celery.conf.broker_transport_options = {
            "socket_timeout": 2, "socket_connect_timeout": 2,
        }
        self._redis = redis.Redis.from_url(broker_url, socket_timeout=1, socket_connect_timeout=1)

    def enqueue(self, parent_asin: str, event_id: str, correlation_id: str) -> bool:
        """Queue a sync job. Returns False (and logs) if the queue is unreachable."""
        try:
            self.celery.send_task(
                SYNC_TASK_NAME,
                args=[parent_asin],
                kwargs={"event_id": event_id, "correlation_id": correlation_id},
                queue=SYNC_QUEUE,
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0,
                              "interval_step": 0.5, "interval_max": 1},
            )
            return True
        except Exception as exc:
            # Postgres already has the change; the worker's reconciliation
            # sweep (Step 11) will still bring Qdrant up to date.
            logger.error("enqueue failed", extra={"parent_asin": parent_asin,
                                                  "event_id": event_id, "error": str(exc)})
            return False

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def close(self) -> None:
        self._redis.close()