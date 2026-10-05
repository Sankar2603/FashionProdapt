"""
Celery application for the catalogue worker and the beat scheduler.

    worker:  celery -A app.celery_app worker --pool=solo -Q catalog_sync,maintenance
    beat:    celery -A app.celery_app beat

--pool=solo runs one task at a time in the main process: the embedding model
is loaded once, and two syncs for the same product can never overlap inside
one worker. (A per-product Redis lock in tasks.py also protects against
overlap if you run several workers.)
"""

import os

from celery import Celery
from celery.signals import worker_ready

from fashion_common.catalog import SYNC_QUEUE
from fashion_common.logging_setup import configure_logging

MAINTENANCE_QUEUE = "maintenance"

configure_logging("worker")

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
RECONCILE_INTERVAL_S = int(os.getenv("RECONCILE_INTERVAL_S", "300"))          # every 5 min
RECONCILE_WINDOW_MIN = int(os.getenv("RECONCILE_WINDOW_MIN", "30"))            # look back 30 min
FULL_RECONCILE_INTERVAL_S = int(os.getenv("FULL_RECONCILE_INTERVAL_S", "21600"))  # every 6 h

celery_app = Celery("worker", broker=BROKER_URL, include=["app.tasks"])
celery_app.conf.update(
    task_default_queue=SYNC_QUEUE,
    task_routes={"worker.reconcile": {"queue": MAINTENANCE_QUEUE}},
    # Acknowledge a job only AFTER it finishes: if the worker crashes mid-job,
    # Redis hands the job out again instead of losing it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,         # take one job at a time
    task_ignore_result=True,              # no result backend needed
    worker_hijack_root_logger=False,      # keep our JSON logging
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "reconcile-recent": {
            "task": "worker.reconcile",
            "schedule": RECONCILE_INTERVAL_S,
            "kwargs": {"since_minutes": RECONCILE_WINDOW_MIN},
        },
        "reconcile-full": {
            "task": "worker.reconcile",
            "schedule": FULL_RECONCILE_INTERVAL_S,
            "kwargs": {"since_minutes": None},
        },
    },
)


@worker_ready.connect
def preload_model(**_):
    """Load the embedding model when the worker starts, not on the first job."""
    from app.tasks import get_syncer
    get_syncer().embedder