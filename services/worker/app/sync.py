"""
Keeps Qdrant in step with Postgres, one product at a time.

Postgres is the source of truth. For a product, the worker compares the
current Postgres row with the current Qdrant point and picks ONE action:

    product row          Qdrant point                       action
    ------------------   --------------------------------   -----------------------------
    missing              exists                             remove   (delete the point)
    is_deleted           exists, not marked deleted         delete   (payload is_deleted=true)
    active               missing                            index    (embed + upsert)
    active               content_hash differs               reembed  (embed + upsert)
    active               other payload fields differ        payload  (update payload only)
    anything else                                           noop

Only index/reembed run the embedding model, so price and rating changes stay
cheap. Because the decision is made from CURRENT state on both sides,
running a sync twice, or for an old event, is harmless.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from qdrant_client import models
from sqlalchemy import select

from fashion_common.database import make_engine, make_session_factory
from fashion_common.models import Product
from fashion_common.vector_store import (active_alias, build_payload, build_point, make_client,
                                         point_id, resolve_alias)

logger = logging.getLogger("worker.sync")

# Changes on every write, so it must not count as "payload differs".
IGNORED_PAYLOAD_FIELDS = {"updated_at"}


@dataclass
class SyncResult:
    parent_asin: str
    action: str                 # remove | delete | index | reembed | payload | noop
    collection: str
    lag_ms: float | None        # Postgres write -> Qdrant updated (None for noop)


def payload_differs(wanted: dict, current: dict) -> bool:
    return any(current.get(key) != value
               for key, value in wanted.items() if key not in IGNORED_PAYLOAD_FIELDS)


def plan(product: Product | None, point) -> str:
    """Decide what to do from the Postgres row and the Qdrant point (either may be None)."""
    if product is None:
        return "remove" if point is not None else "noop"
    if product.is_deleted:
        if point is None or point.payload.get("is_deleted"):
            return "noop"
        return "delete"
    if point is None:
        return "index"
    if point.payload.get("content_hash") != product.content_hash:
        return "reembed"
    if payload_differs(build_payload(product), point.payload):
        return "payload"
    return "noop"


def _default_embedder():
    from fashion_common.embedder import Embedder   # imports PyTorch: only when needed
    return Embedder()


class Syncer:
    def __init__(self, embedder_factory: Callable | None = None):
        self.engine = make_engine()
        self.Session = make_session_factory(self.engine)
        self.qdrant = make_client()
        self._embedder_factory = embedder_factory or _default_embedder
        self._embedder = None

    # ----------------------------------------------------------- helpers
    @property
    def embedder(self):
        """Loaded on first use (or at worker start-up), then kept in memory."""
        if self._embedder is None:
            logger.info("loading embedding model")
            self._embedder = self._embedder_factory()
            logger.info("embedding model loaded")
        return self._embedder

    def collection(self) -> str:
        """The collection the active alias points to: the one search reads."""
        name = resolve_alias(self.qdrant, active_alias())
        if name is None:
            raise RuntimeError(f"Qdrant alias {active_alias()!r} not found. "
                               "Run python -m ingestion.index_qdrant first.")
        return name

    def load_product(self, parent_asin: str) -> Product | None:
        with self.Session() as session:
            return session.get(Product, parent_asin)

    def get_point(self, collection: str, parent_asin: str):
        points = self.qdrant.retrieve(collection, ids=[point_id(parent_asin)],
                                      with_payload=True, with_vectors=False)
        return points[0] if points else None

    # -------------------------------------------------------------- sync
    def sync(self, parent_asin: str) -> SyncResult:
        collection = self.collection()
        product = self.load_product(parent_asin)
        point = self.get_point(collection, parent_asin)
        action = plan(product, point)

        if action in ("index", "reembed"):
            encoded = self.embedder.encode([product.search_text])[0]
            self.qdrant.upsert(collection, points=[build_point(product, encoded)], wait=True)
        elif action in ("payload", "delete"):
            # set_payload merges: listed keys are overwritten, the vectors are untouched.
            self.qdrant.set_payload(collection, payload=build_payload(product),
                                    points=[point_id(parent_asin)], wait=True)
        elif action == "remove":
            self.qdrant.delete(collection,
                               points_selector=models.PointIdsList(points=[point_id(parent_asin)]),
                               wait=True)

        lag_ms = None
        if action != "noop" and product is not None and product.updated_at is not None:
            lag_ms = round((datetime.now(timezone.utc) - product.updated_at).total_seconds() * 1000, 1)
        return SyncResult(parent_asin, action, collection, lag_ms)

    # --------------------------------------------------------- reconcile
    def reconcile(self, since_minutes: int | None, enqueue: Callable[[str], None],
                  batch_size: int = 500) -> dict:
        """
        Compare Postgres with Qdrant and enqueue a sync for every product that
        doesn't match. since_minutes=None checks every product; otherwise only
        products changed in that window. Reads in batches, so memory stays flat.
        """
        collection = self.collection()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
                  if since_minutes else None)
        counts: Counter = Counter()
        last_asin = ""

        while True:
            query = (select(Product).where(Product.parent_asin > last_asin)
                     .order_by(Product.parent_asin).limit(batch_size))
            if cutoff is not None:
                query = query.where(Product.updated_at >= cutoff)
            with self.Session() as session:
                products = list(session.scalars(query))
            if not products:
                break
            last_asin = products[-1].parent_asin

            points = self.qdrant.retrieve(collection,
                                          ids=[point_id(p.parent_asin) for p in products],
                                          with_payload=True, with_vectors=False)
            by_id = {str(p.id): p for p in points}

            for product in products:
                action = plan(product, by_id.get(point_id(product.parent_asin)))
                counts["checked"] += 1
                if action == "noop":
                    counts["in_sync"] += 1
                else:
                    counts[action] += 1
                    enqueue(product.parent_asin)

        summary = {"collection": collection,
                   "scope": f"last {since_minutes} min" if since_minutes else "full",
                   **counts}
        logger.info("reconcile finished", extra=summary)
        return summary