
import argparse
import os
import time

from dotenv import load_dotenv
from sqlalchemy import func, select

from fashion_common.catalog import bump_catalog_version
from fashion_common.database import make_engine, make_session_factory
from fashion_common.embedder import Embedder
from fashion_common.models import Product
from fashion_common.vector_store import (
    active_alias,
    build_point,
    create_collection,
    make_client,
    resolve_alias,
    set_alias,
    upsert_points,
)

load_dotenv()


def load_products(limit: int | None) -> list[Product]:
    Session = make_session_factory(make_engine())
    with Session() as session:
        query = (
            select(Product)
            .where(Product.is_deleted.is_(False))
            .order_by(Product.parent_asin)
        )
        if limit:
            query = query.limit(limit)
        return list(session.scalars(query))


def format_eta(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed products and index them in Qdrant.")
    parser.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "fashion_items_v1"))
    parser.add_argument("--limit", type=int, default=0, help="Only index the first N products (0 = all)")
    parser.add_argument("--batch-size", type=int, default=32, help="Products per embed+upsert batch")
    parser.add_argument("--recreate", action="store_true", help="Delete and rebuild the collection first")
    parser.add_argument("--no-alias", action="store_true", help="Don't move the active alias")
    args = parser.parse_args()

    # 1. Products from the source of truth.
    products = load_products(args.limit or None)
    if not products:
        raise SystemExit("No active products in Postgres. Run ingestion.load_postgres first.")
    print(f"Products to index: {len(products):,}")

    # 2. Qdrant collection.
    client = make_client()
    created = create_collection(client, args.collection, recreate=args.recreate)
    print(f"Collection {args.collection}: {'created' if created else 'exists, upserting into it'}")

    # 3. Model (first run downloads it).
    print("Loading embedding model (the first run downloads ~2.3 GB)...")
    load_start = time.perf_counter()
    embedder = Embedder()
    print(f"Model {embedder.model_name} loaded on {embedder.device} "
          f"in {time.perf_counter() - load_start:.1f}s")

    # 4. Embed + upsert in batches.
    start = time.perf_counter()
    embed_seconds = 0.0
    done = 0
    for i in range(0, len(products), args.batch_size):
        batch = products[i:i + args.batch_size]

        t0 = time.perf_counter()
        encoded = embedder.encode([p.search_text for p in batch])
        embed_seconds += time.perf_counter() - t0

        points = [build_point(p, e) for p, e in zip(batch, encoded)]
        upsert_points(client, args.collection, points)

        done += len(batch)
        elapsed = time.perf_counter() - start
        rate = done / elapsed
        eta = (len(products) - done) / rate if rate else 0
        print(f"  {done:>6,}/{len(products):,}  {rate:5.1f} products/s  ETA {format_eta(eta)}")

    total_seconds = time.perf_counter() - start
    count = client.count(args.collection, exact=True).count

    # 5. Alias.
    if not args.no_alias:
        previous = resolve_alias(client, active_alias())
        set_alias(client, active_alias(), args.collection)
        # The alias decides what search sees: invalidate cached search results.
        bump_catalog_version()
        print(f"\nAlias {active_alias()}: {previous or '(none)'} -> {args.collection}")

    print(f"\nIndexed this run:         {done:,}")
    print(f"Points in collection:     {count:,}")
    print(f"Embedding throughput:     {done / embed_seconds:.1f} products/s (model time only)")
    print(f"Overall throughput:       {done / total_seconds:.1f} products/s (embed + upsert)")
    print(f"Time:                     {format_eta(total_seconds)}")

    # Rough projection for the whole catalog in Postgres.
    if args.limit:
        Session = make_session_factory(make_engine())
        with Session() as session:
            total = session.scalar(
                select(func.count()).select_from(Product).where(Product.is_deleted.is_(False))
            )
        print(f"Projected for all {total:,} active products: "
              f"~{format_eta(total / (done / total_seconds))} at this speed")


if __name__ == "__main__":
    main()