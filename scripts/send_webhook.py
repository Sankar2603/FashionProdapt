"""
Sends signed test webhooks to the Catalog Service.

Examples (from the project root):
    python scripts/send_webhook.py show   B07SB2892S
    python scripts/send_webhook.py price  B07SB2892S 19.99
    python scripts/send_webhook.py edit   B07SB2892S --title "Linen Beach Shirt for Men"
    python scripts/send_webhook.py new    --title "Women's Linen Midi Dress" --price 34.5 --store "Demo Brand"
    python scripts/send_webhook.py delete B07SB2892S

Options for any event command:
    --repeat 2          send the SAME event twice (tests idempotency -> "duplicate")
    --age-seconds 3600  pretend the change happened an hour ago (tests "stale")
    --bad-signature     sign with the wrong secret (tests 401)

price/edit fetch the current product first and send a full snapshot with
your change applied, the way a real source system would.
"""

import argparse
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from fashion_common.webhooks import signed_headers

load_dotenv()

CATALOG_URL = os.getenv("CATALOG_URL", "http://127.0.0.1:8002").rstrip("/")
SNAPSHOT_FIELDS = ("parent_asin", "title", "category", "store", "features", "description",
                   "image_url", "price", "average_rating", "rating_number", "review_count",
                   "avg_review_rating")


def fail(message: str) -> None:
    print(f"Error: {message}")
    sys.exit(1)


def fetch_product(client: httpx.Client, asin: str) -> dict:
    response = client.get(f"{CATALOG_URL}/products/{asin}")
    if response.status_code == 404:
        fail(f"product {asin} not found in the catalog")
    response.raise_for_status()
    return response.json()


def snapshot(product: dict) -> dict:
    return {field: product.get(field) for field in SNAPSHOT_FIELDS}


def send(client: httpx.Client, event: dict, args) -> None:
    body = json.dumps(event).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        fail("WEBHOOK_SECRET is not set in .env")
    if args.bad_signature:
        secret = "not-the-real-secret"

    for attempt in range(1, args.repeat + 1):
        headers = {"Content-Type": "application/json", **signed_headers(secret, body)}
        response = client.post(f"{CATALOG_URL}/webhooks/products", content=body, headers=headers)
        label = f" (delivery {attempt}/{args.repeat})" if args.repeat > 1 else ""
        print(f"\nHTTP {response.status_code}{label}")
        print(json.dumps(response.json(), indent=2))


def make_event(event_type: str, args, product: dict | None = None,
               parent_asin: str | None = None) -> dict:
    occurred_at = datetime.now(timezone.utc) - timedelta(seconds=args.age_seconds)
    event = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
    }
    if product is not None:
        event["product"] = product
    if parent_asin is not None:
        event["parent_asin"] = parent_asin
    return event


def main() -> None:
    parser = argparse.ArgumentParser(description="Send signed test webhooks.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repeat", type=int, default=1, help="Send the same event N times")
    common.add_argument("--age-seconds", type=int, default=0,
                        help="Backdate occurred_at by this many seconds")
    common.add_argument("--bad-signature", action="store_true", help="Sign with a wrong secret")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("show", help="Print the product as stored in Postgres")
    p.add_argument("asin")

    p = sub.add_parser("price", parents=[common], help="Change a product's price")
    p.add_argument("asin")
    p.add_argument("new_price", type=float)

    p = sub.add_parser("edit", parents=[common], help="Change descriptive fields")
    p.add_argument("asin")
    for field in ("title", "description", "features", "store", "category"):
        p.add_argument(f"--{field}")

    p = sub.add_parser("new", parents=[common], help="Create a brand-new product")
    p.add_argument("--asin", help="Default: a random TEST... ID")
    p.add_argument("--title", required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--description", default="")
    p.add_argument("--features", default="")
    p.add_argument("--store")
    p.add_argument("--category")

    p = sub.add_parser("delete", parents=[common], help="Remove a product from search")
    p.add_argument("asin")

    args = parser.parse_args()

    with httpx.Client(timeout=10) as client:
        if args.command == "show":
            print(json.dumps(fetch_product(client, args.asin), indent=2))
            return

        if args.command == "price":
            product = snapshot(fetch_product(client, args.asin))
            print(f"Price {product['price']} -> {args.new_price}")
            product["price"] = args.new_price
            event = make_event("product.upserted", args, product=product)

        elif args.command == "edit":
            product = snapshot(fetch_product(client, args.asin))
            changes = {f: getattr(args, f) for f in ("title", "description", "features",
                                                     "store", "category")
                       if getattr(args, f) is not None}
            if not changes:
                fail("give at least one of --title --description --features --store --category")
            product.update(changes)
            print(f"Changing: {', '.join(changes)}")
            event = make_event("product.upserted", args, product=product)

        elif args.command == "new":
            asin = args.asin or f"TEST{secrets.token_hex(3).upper()}"
            product = {"parent_asin": asin, "title": args.title, "price": args.price,
                       "description": args.description, "features": args.features,
                       "store": args.store, "category": args.category}
            print(f"Creating {asin}")
            event = make_event("product.upserted", args, product=product)

        else:  # delete
            event = make_event("product.deleted", args, parent_asin=args.asin)

        send(client, event, args)


if __name__ == "__main__":
    main()