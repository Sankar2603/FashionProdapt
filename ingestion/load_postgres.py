
import argparse
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from fashion_common.database import make_engine, make_session_factory
from fashion_common.models import Base, Product

load_dotenv()

DEFAULT_INPUT = Path("data/processed/products.jsonl")

# Columns we fill from the JSONL file. Timestamps are set by Postgres.
COLUMNS = [c.name for c in Product.__table__.columns if c.name not in ("created_at", "updated_at")]
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

    # Create the table if it doesn't exist (no-op if it does).
    Base.metadata.create_all(engine)

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