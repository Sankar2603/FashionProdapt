import argparse
import time

from dotenv import load_dotenv

from fashion_common.embedder import Embedder
from fashion_common.vector_store import active_alias, make_client, resolve_alias, search

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Try a search against Qdrant.")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-price", type=float, default=None)
    parser.add_argument("--mode", choices=["hybrid", "dense", "sparse"], default="hybrid")
    args = parser.parse_args()

    client = make_client()
    alias = active_alias()
    collection = resolve_alias(client, alias)
    if collection is None:
        raise SystemExit(f"Alias {alias!r} not found. Run ingestion.index_qdrant first.")

    print("Loading embedding model...")
    embedder = Embedder()

    t0 = time.perf_counter()
    encoded = embedder.encode([args.query])[0]
    encode_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    hits = search(client, alias, encoded, limit=args.limit, max_price=args.max_price, mode=args.mode)
    search_ms = (time.perf_counter() - t1) * 1000

    print(f"\nQuery: {args.query!r}  mode={args.mode}  max_price={args.max_price}  "
          f"({alias} -> {collection})")
    print(f"Encode {encode_ms:.0f} ms, search {search_ms:.0f} ms\n")
    if not hits:
        print("No results.")
    for rank, hit in enumerate(hits, start=1):
        p = hit.payload
        print(f"{rank}. [{hit.score:.4f}] ${p['price']:<7} rating {p['bayesian_score']:.2f}  "
              f"{p['title'][:70]}")


if __name__ == "__main__":
    main()