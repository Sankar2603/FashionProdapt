"""
Run a reconciliation now and print what it found (it queues the fixes).

Inside the worker container:
    docker compose exec worker python -m app.run_reconcile            # every product
    docker compose exec worker python -m app.run_reconcile --since 30 # changed in last 30 min

Use it after an alias switch or after the worker was down for a while.
"""

import argparse
import json

from app.tasks import enqueue_sync, get_syncer


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Postgres with Qdrant and queue fixes.")
    parser.add_argument("--since", type=int, default=None,
                        help="Only products changed in the last N minutes (default: all)")
    args = parser.parse_args()

    summary = get_syncer().reconcile(args.since, enqueue=enqueue_sync)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()