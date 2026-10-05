"""
Loads clean products into Postgres (the source of truth).

Creates the tables if they do not exist (and adds any newer columns), then
upserts every product from data/processed/products.jsonl: new parent_asins
are inserted, existing ones are updated. Safe to re-run.

Run from the project root:
    python -m ingestion.load_postgres
"""

import argparse
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from fashion_common.database import make_engine, make_session_factory
from fashion_common.models import Product, ensure_schema

load_dotenv()

DEFAULT_INPUT = Path("data/processed/products.jsonl")

# Columns we fill from the JSONL file. Timestamps are set by Postgres, and
# source_updated_at belongs to the webhook flow, so the batch load leaves it alone.
SKIP_COLUMNS = {"created_at", "updated_at", "source_updated_at"}
COLUMNS = [c.name for c in Product.__table__.columns if c.name not in SKIP_COLUMNS]
# On conflict, update everything except the primary key.
UPDATE_COLUMNS = [c for c in COLUMNS if c != "parent_asin"]


def read_products(path: Path):
    """Yield product dicts containing only the table's columns."""
    with path.open("r", encoding="utf-8") as src:
        for line in src:
            record = json.loads(line)
            yield {col: record.get(col) for col in COLUMNS}


def upsert_batch(session, rows: list[dict]) -> None:
    """INSERT ... ON CONFLICT (parent_asin) DO UPDATE for one batch."""
    stmt = insert(Product).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Product.parent_asin],
        set_={**{col: stmt.excluded[col] for col in UPDATE_COLUMNS}, "updated_at": func.now()},
    )
    session.execute(stmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load products into Postgres.")
    parser.add_argument("--file", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file.resolve()} (run ingestion.prepare first)")

    engine = make_engine()
    Session = make_session_factory(engine)

    # Create missing tables / columns (no-op if they exist).
    ensure_schema(engine)

    start = time.perf_counter()
    processed = 0
    batch: list[dict] = []

    with Session() as session:
        for row in read_products(args.file):
            batch.append(row)
            if len(batch) >= args.batch_size:
                upsert_batch(session, batch)
                session.commit()
                processed += len(batch)
                print(f"  ... {processed:,} rows upserted")
                batch = []
        if batch:
            upsert_batch(session, batch)
            session.commit()
            processed += len(batch)

        total = session.scalar(select(func.count()).select_from(Product))
        active = session.scalar(
            select(func.count()).select_from(Product).where(Product.is_deleted.is_(False))
        )

    elapsed = time.perf_counter() - start
    print(f"\nRows upserted this run:  {processed:,}")
    print(f"Products in table:       {total:,} ({active:,} active)")
    print(f"Time:                    {elapsed:.1f}s")


if __name__ == "__main__":
    main()