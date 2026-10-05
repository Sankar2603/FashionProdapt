"""
Catalog Service (port 8002): the live entry point for catalogue changes.

POST /webhooks/products
    1. verify the HMAC signature           -> 401 if missing / wrong / too old
    2. validate the event (Pydantic)       -> 422 if malformed
    3. write Postgres in one transaction   (idempotent + out-of-order safe)
    4. queue a sync job for the worker     -> 202 Accepted
GET  /products/{parent_asin}               -> the current row in Postgres

Qdrant is NOT touched here; the Celery worker (Step 11) does that.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from fashion_common.errors import ServiceError
from fashion_common.middleware import get_correlation_id
from fashion_common.service_app import create_app
from fashion_common.webhooks import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, verify

from app.schemas import ProductOut, WebhookEvent, WebhookResponse
from app.store import CatalogStore, build_record
from app.sync_queue import SyncQueue

SERVICE_NAME = "catalog_service"
logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        # Refuse to run an unauthenticated write endpoint.
        raise RuntimeError("WEBHOOK_SECRET is not set. Add it to your .env file.")
    app.state.secret = secret
    app.state.tolerance_s = int(os.getenv("WEBHOOK_TOLERANCE_S", "300"))

    app.state.store = CatalogStore()
    await run_in_threadpool(app.state.store.setup)       # create tables / columns if missing
    app.state.queue = SyncQueue(os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1"))
    logger.info("catalog service ready")
    yield
    app.state.queue.close()
    app.state.store.engine.dispose()


async def health_check(app: FastAPI) -> dict[str, bool]:
    return {
        "postgres": await run_in_threadpool(app.state.store.ping),
        "queue": await run_in_threadpool(app.state.queue.ping),
    }


app = create_app(SERVICE_NAME, lifespan=lifespan, health_check=health_check)


def _validation_details(exc: ValidationError) -> list[dict]:
    return exc.errors(include_url=False, include_context=False, include_input=False)


@app.post("/webhooks/products", response_model=WebhookResponse,
          responses={202: {"description": "Applied and queued for indexing"},
                     200: {"description": "Duplicate, stale or unknown product; nothing to do"},
                     401: {"description": "Bad signature"}, 422: {"description": "Bad payload"}})
async def receive_product_event(request: Request, response: Response) -> WebhookResponse:
    state = request.app.state

    # 1. Signature: computed over the RAW bytes, so read them before parsing.
    body = await request.body()
    try:
        verify(state.secret, request.headers.get(TIMESTAMP_HEADER),
               request.headers.get(SIGNATURE_HEADER), body, state.tolerance_s)
    except SignatureError as exc:
        raise ServiceError(401, "invalid_signature", str(exc)) from None

    # 2. Validate the event, and for upserts build the clean, validated record.
    try:
        event = WebhookEvent.model_validate_json(body)
    except ValidationError as exc:
        raise ServiceError(422, "validation_error", "Webhook body is invalid.",
                           _validation_details(exc)) from None

    record = None
    if event.event_type == "product.upserted":
        C = await run_in_threadpool(state.store.catalog_mean)
        try:
            record = build_record(event.product, C, state.store.bayes_m)
        except ValidationError as exc:
            raise ServiceError(422, "validation_error", "Product failed validation.",
                               _validation_details(exc)) from None

    # 3. Postgres (one transaction).
    result = await run_in_threadpool(state.store.apply_event, event, record)

    # 4. Queue the sync job, only after the change is committed.
    queued = False
    if result.status == "applied":
        queued = await run_in_threadpool(
            state.queue.enqueue, event.parent_asin, event.event_id, get_correlation_id())

    response.status_code = 202 if result.status == "applied" else 200
    logger.info("webhook processed", extra={
        "event_id": event.event_id, "event_type": event.event_type,
        "parent_asin": event.parent_asin, "status": result.status,
        "change": result.change, "queued": queued,
    })
    return WebhookResponse(event_id=event.event_id, parent_asin=event.parent_asin,
                           status=result.status, change=result.change, queued=queued)


@app.get("/products/{parent_asin}", response_model=ProductOut)
async def get_product(parent_asin: str, request: Request) -> ProductOut:
    product = await run_in_threadpool(request.app.state.store.get_product, parent_asin)
    if product is None:
        raise ServiceError(404, "not_found", f"No product with parent_asin {parent_asin!r}.")
    return ProductOut.model_validate(product, from_attributes=True)