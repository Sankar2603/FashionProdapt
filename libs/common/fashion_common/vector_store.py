"""
Qdrant helpers shared by the indexer, the Retrieval Service and the worker.

Each product is one Qdrant point:
    id      = uuid5(parent_asin)      (Qdrant ids must be UUIDs or integers)
    vectors = {"dense": [...1024...], "sparse": {indices, values}}
    payload = product fields (title, price, bayesian_score, is_deleted, ...)

Services always search the alias (fashion_items_active), never a versioned
collection name, so a new collection can be swapped in without code changes.
"""

import os
import uuid

from qdrant_client import QdrantClient, models

from fashion_common.embedder import DENSE_DIM, EncodedText

DENSE = "dense"
SPARSE = "sparse"

# Fixed namespace: the same parent_asin always maps to the same point id.
_ID_NAMESPACE = uuid.UUID("6f1c2a52-6c1b-4f0e-9a51-3c8f3b1d2e77")

PAYLOAD_FIELDS = [
    "parent_asin", "title", "category", "store", "image_url",
    "price", "average_rating", "rating_number", "review_count", "bayesian_score",
    "search_text", "content_hash", "is_deleted",
]


def active_alias() -> str:
    return os.getenv("QDRANT_ALIAS", "fashion_items_active")


def make_client() -> QdrantClient:
    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    api_key = os.getenv("QDRANT_API_KEY") or None
    return QdrantClient(url=url, api_key=api_key, timeout=30)


def point_id(parent_asin: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, parent_asin))


# ---------------------------------------------------------------------------
# Collections and aliases
# ---------------------------------------------------------------------------
def create_collection(client: QdrantClient, name: str, recreate: bool = False) -> bool:
    """Create the collection if missing. Returns True if it was created."""
    if client.collection_exists(name):
        if not recreate:
            return False
        client.delete_collection(name)

    client.create_collection(
        collection_name=name,
        vectors_config={DENSE: models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams()},
    )
    # Indexes on fields we filter by keep filtered search fast.
    client.create_payload_index(name, "is_deleted", models.PayloadSchemaType.BOOL)
    client.create_payload_index(name, "price", models.PayloadSchemaType.FLOAT)
    return True


def resolve_alias(client: QdrantClient, alias: str) -> str | None:
    """Which collection does the alias point to? None if the alias doesn't exist."""
    for item in client.get_aliases().aliases:
        if item.alias_name == alias:
            return item.collection_name
    return None


def set_alias(client: QdrantClient, alias: str, collection: str) -> None:
    """Point alias -> collection in ONE atomic request (no moment without an alias)."""
    operations = []
    if resolve_alias(client, alias) is not None:
        operations.append(
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
def build_payload(product) -> dict:
    """Payload from a Product ORM object (or anything with the same attributes)."""
    payload = {field: getattr(product, field) for field in PAYLOAD_FIELDS}
    updated_at = getattr(product, "updated_at", None)
    payload["updated_at"] = updated_at.isoformat() if updated_at else None
    return payload


def build_point(product, encoded: EncodedText) -> models.PointStruct:
    return models.PointStruct(
        id=point_id(product.parent_asin),
        vector={
            DENSE: encoded.dense,
            SPARSE: models.SparseVector(indices=encoded.sparse_indices, values=encoded.sparse_values),
        },
        payload=build_payload(product),
    )


def upsert_points(client: QdrantClient, collection: str, points: list[models.PointStruct]) -> None:
    # wait=True: return only once the points are stored and searchable.
    client.upsert(collection_name=collection, points=points, wait=True)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def build_filter(max_price: float | None = None) -> models.Filter:
    """Always hide deleted products; optionally cap the price."""
    must = [models.FieldCondition(key="is_deleted", match=models.MatchValue(value=False))]
    if max_price is not None:
        must.append(models.FieldCondition(key="price", range=models.Range(lte=max_price)))
    return models.Filter(must=must)


def search(
    client: QdrantClient,
    collection: str,
    encoded: EncodedText,
    limit: int = 20,
    max_price: float | None = None,
    mode: str = "hybrid",
    with_vectors: bool = False,
) -> list[models.ScoredPoint]:
    """
    mode = "hybrid": dense search + sparse search, fused with RRF (inside Qdrant)
    mode = "dense" or "sparse": one kind only (useful for comparing)
    with_vectors = True also returns each hit's dense vector (used for MMR).
    """
    query_filter = build_filter(max_price)
    vectors = [DENSE] if with_vectors else False
    sparse_query = models.SparseVector(indices=encoded.sparse_indices, values=encoded.sparse_values)

    if mode == "dense":
        result = client.query_points(
            collection, query=encoded.dense, using=DENSE,
            query_filter=query_filter, limit=limit, with_payload=True, with_vectors=vectors,
        )
    elif mode == "sparse":
        result = client.query_points(
            collection, query=sparse_query, using=SPARSE,
            query_filter=query_filter, limit=limit, with_payload=True, with_vectors=vectors,
        )
    elif mode == "hybrid":
        # Each prefetch gathers candidates with the SAME filter; RRF merges the two rankings.
        prefetch_limit = max(limit * 2, 50)
        result = client.query_points(
            collection,
            prefetch=[
                models.Prefetch(query=encoded.dense, using=DENSE, filter=query_filter, limit=prefetch_limit),
                models.Prefetch(query=sparse_query, using=SPARSE, filter=query_filter, limit=prefetch_limit),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
            with_vectors=vectors,
        )
    else:
        raise ValueError(f"Unknown mode: {mode!r} (use hybrid, dense or sparse)")
    return result.points