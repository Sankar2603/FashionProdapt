import argparse
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_META_FILE = "meta_Amazon_Fashion.jsonl"
DEFAULT_REVIEWS_FILE = "Amazon_Fashion.jsonl"
OUTPUT_DIR = Path("data/dev")
PROGRESS_EVERY = 200_000  # print a progress line every N lines

# Kids detection: department field first, then title words.
# Titles like "Earrings for Women Girls" are adult, so an adult word wins.
_ADULT_WORDS = re.compile(r"\b(men'?s?|women'?s?|womens|ladies|adults?)\b", re.IGNORECASE)
_KIDS_WORDS = re.compile(
    r"\b(boys?'?s?|girls'|girl's|kids?'?|toddlers?|baby|babies|infants?|youth|newborn)\b",
    re.IGNORECASE,
)
_KIDS_AGE = re.compile(r"\b\d{1,2}\s*-\s*\d{1,2}\s*(years|yrs)\b", re.IGNORECASE)
_KIDS_DEPARTMENTS = ("boy", "girl", "baby", "kid", "infant", "toddler")


def parse_price(value) -> float | None:
    """Prices are usually floats, sometimes strings like '$12.99', often null."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        text = str(value).replace("$", "").replace(",", "").strip()
        try:
            price = float(text)
        except ValueError:
            return None
    return price if price > 0 else None


def is_kids(record: dict) -> bool:
    """True if the product looks like a children's product."""
    details = record.get("details")
    if isinstance(details, dict):
        department = str(details.get("Department", "")).lower()
        if any(word in department for word in _KIDS_DEPARTMENTS):
            return True

    title = record.get("title") or ""
    if _ADULT_WORDS.search(title):
        return False
    return bool(_KIDS_WORDS.search(title) or _KIDS_AGE.search(title))


def sample_meta(meta_path: Path, out_path: Path, max_items: int, exclude_kids: bool) -> set[str]:
    """Pass 1: keep valid products. Returns the set of kept parent_asins."""
    skipped: Counter = Counter()
    kept_asins: set[str] = set()
    lines_read = 0

    with meta_path.open("r", encoding="utf-8") as src, out_path.open("w", encoding="utf-8") as dst:
        for line in src:
            lines_read += 1
            if lines_read % PROGRESS_EVERY == 0:
                print(f"  ... {lines_read:,} lines read, {len(kept_asins):,} kept")

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped["bad_json"] += 1
                continue

            asin = record.get("parent_asin")
            if not asin:
                skipped["no_parent_asin"] += 1
                continue
            if asin in kept_asins:
                skipped["duplicate"] += 1
                continue
            if not str(record.get("title") or "").strip():
                skipped["no_title"] += 1
                continue
            if parse_price(record.get("price")) is None:
                skipped["no_price"] += 1
                continue
            if exclude_kids and is_kids(record):
                skipped["kids"] += 1
                continue

            dst.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept_asins.add(asin)

            if max_items and len(kept_asins) >= max_items:
                print(f"  Reached --max-items {max_items:,}; stopping early.")
                break

    print(f"\nMeta lines read: {lines_read:,}")
    print(f"Products kept:   {len(kept_asins):,}")
    print("\nSkipped:")
    if not skipped:
        print("  (none)")
    for reason, count in skipped.most_common():
        print(f"  {reason:<16} {count:>10,}")
    return kept_asins


def sample_reviews(reviews_path: Path, out_path: Path, kept_asins: set[str]) -> None:
    """Pass 2: keep reviews whose parent_asin was kept in pass 1."""
    lines_read = 0
    kept = 0
    bad_json = 0
    products_with_reviews: set[str] = set()

    with reviews_path.open("r", encoding="utf-8") as src, out_path.open("w", encoding="utf-8") as dst:
        for line in src:
            lines_read += 1
            if lines_read % PROGRESS_EVERY == 0:
                print(f"  ... {lines_read:,} lines read, {kept:,} kept")

            try:
                review = json.loads(line)
            except json.JSONDecodeError:
                bad_json += 1
                continue

            asin = review.get("parent_asin")
            if asin in kept_asins:
                dst.write(json.dumps(review, ensure_ascii=False) + "\n")
                kept += 1
                products_with_reviews.add(asin)

    print(f"\nReview lines read:           {lines_read:,}")
    print(f"Reviews kept:                {kept:,}")
    print(f"Kept products with reviews:  {len(products_with_reviews):,} of {len(kept_asins):,}")
    if bad_json:
        print(f"Bad JSON lines skipped:      {bad_json:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the dev dataset.")
    parser.add_argument("--raw-dir", default=os.getenv("RAW_DATA_DIR", "."),
                        help="Folder containing the two raw .jsonl files")
    parser.add_argument("--meta-file", default=DEFAULT_META_FILE)
    parser.add_argument("--reviews-file", default=DEFAULT_REVIEWS_FILE)
    parser.add_argument("--max-items", type=int, default=int(os.getenv("SAMPLE_MAX_ITEMS", "5000")),
                        help="Stop after keeping this many products (0 = no limit)")
    parser.add_argument("--include-kids", action="store_true",
                        help="Keep kids' products (excluded by default)")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    meta_path = raw_dir / args.meta_file
    reviews_path = raw_dir / args.reviews_file
    for path in (meta_path, reviews_path):
        if not path.exists():
            raise SystemExit(f"File not found: {path.resolve()}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    meta_out = OUTPUT_DIR / "meta_dev.jsonl"
    reviews_out = OUTPUT_DIR / "reviews_dev.jsonl"
    exclude_kids = not args.include_kids

    start = time.perf_counter()
    limit = f"{args.max_items:,}" if args.max_items else "no limit"
    print(f"Pass 1: sampling products from {meta_path} (max {limit}, "
          f"kids {'excluded' if exclude_kids else 'included'})")
    kept_asins = sample_meta(meta_path, meta_out, args.max_items, exclude_kids)
    pass1_s = time.perf_counter() - start

    print(f"\nPass 2: filtering reviews from {reviews_path}")
    sample_reviews(reviews_path, reviews_out, kept_asins)
    total_s = time.perf_counter() - start

    print(f"\nWrote {meta_out} and {reviews_out}")
    print(f"Time: pass 1 {pass1_s:.1f}s, total {total_s:.1f}s")


if __name__ == "__main__":
    main()