"""
Points the active alias at another collection (e.g. after building v2).

Run from the project root:
    python scripts/switch_alias.py fashion_items_v2
"""

import sys

from dotenv import load_dotenv

from fashion_common.vector_store import active_alias, make_client, resolve_alias, set_alias

load_dotenv()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/switch_alias.py <collection_name>")
    target = sys.argv[1]

    client = make_client()
    if not client.collection_exists(target):
        raise SystemExit(f"Collection {target!r} does not exist.")
    points = client.count(target, exact=True).count
    if points == 0:
        raise SystemExit(f"Collection {target!r} is empty; refusing to switch.")

    alias = active_alias()
    previous = resolve_alias(client, alias)
    set_alias(client, alias, target)
    print(f"{alias}: {previous or '(none)'} -> {target} ({points:,} points)")


if __name__ == "__main__":
    main()