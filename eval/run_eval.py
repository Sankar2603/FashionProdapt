"""
End-to-end evaluation of the search system.

For every query in eval/queries.jsonl:
  1. /parse (Query Service)   -> price accuracy, intent quality, LLM latency
  2. /retrieve hybrid, dense, sparse (Retrieval Service, top 20 each)
  3. /search (Gateway)        -> the final top 5 after reranking, total latency
  4. LLM judge labels every product in the pool (union of step 2) 0/1/2
  5. nDCG@5 (retrieval order vs reranked), Recall@20, price violations

Judge labels are cached in eval/results/labels.json, so re-runs only judge
new products. A random sample is written to eval/results/spot_check.csv for
you to label by hand; --agreement then reports how often you and the judge agree.

Run from the project root, with all services up (docker compose up -d):
    python -m eval.run_eval                 # full run
    python -m eval.run_eval --limit 5       # quick trial on 5 queries
    python -m eval.run_eval --agreement     # after filling human_label in spot_check.csv
"""

import argparse
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

ROOT = Path(__file__).resolve().parent
QUERIES_FILE = ROOT / "queries.jsonl"
RESULTS_DIR = ROOT / "results"
LABELS_FILE = RESULTS_DIR / "labels.json"

GATEWAY_URL = os.getenv("EVAL_GATEWAY_URL", "http://127.0.0.1:8000")
QUERY_URL = os.getenv("EVAL_QUERY_URL", "http://127.0.0.1:8001")
RETRIEVAL_URL = os.getenv("EVAL_RETRIEVAL_URL", "http://127.0.0.1:8003")
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", os.getenv("LLM_MODEL", "openai/gpt-oss-120b"))

TOP_K = 20          # retrieval candidates (matches RETRIEVE_TOP_K)
TOP_N = 5           # results shown to the user
JUDGE_CHUNK = 15    # products per judge call (keeps each call small for Groq's limits)
TEXT_CHARS = 300    # how much of each product's search_text the judge sees
SPOT_CHECK_SIZE = 30
PARSE_PAUSE_S = 1.0  # pause between /parse calls, to stay under Groq's rate limit


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def post(client: httpx.Client, url: str, payload: dict, attempts: int = 5) -> dict:
    """POST JSON; on 429 wait for Retry-After and try again."""
    for _ in range(attempts):
        response = client.post(url, json=payload)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "10")) + 1
            print(f"    rate limited by {url}; waiting {wait}s")
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Still rate limited after {attempts} attempts: {url}")


def clear_caches() -> int:
    """
    Delete cached intents (so /parse measures real LLM calls) and cached search
    results (so /search runs the full pipeline). Returns keys deleted.
    """
    import redis
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    keys = list(client.scan_iter("intent:*")) + list(client.scan_iter("search:*"))
    if keys:
        client.delete(*keys)
    return len(keys)


# ---------------------------------------------------------------------------
# Parser metrics
# ---------------------------------------------------------------------------
def price_correct(expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    return abs(float(expected) - float(actual)) < 0.005


def intent_checks(case: dict, parsed: dict) -> dict:
    """Which parts of intent quality passed for one query."""
    phrase = parsed["search_query"].lower()
    missing = []
    for item in case.get("must_contain", []):
        options = item if isinstance(item, list) else [item]
        if not any(option.lower() in phrase for option in options):
            missing.append("/".join(options))
    forbidden = [w for w in case.get("must_not_contain", []) if w.lower() in phrase]

    leaked = "$" in phrase or bool(re.search(r"\b(dollars?|usd|bucks|dólares)\b", phrase))
    if case.get("expected_price") is not None:
        amount = f"{float(case['expected_price']):g}"
        leaked = leaked or bool(re.search(rf"\b{re.escape(amount)}\b", phrase))

    language_ok = parsed["language"].lower() == case["expected_language"]
    return {
        "missing_words": missing,
        "forbidden_words": forbidden,
        "price_leaked": leaked,
        "language_ok": language_ok,
        "passed": not missing and not forbidden and not leaked and language_ok,
    }


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------
def dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    """
    nDCG@k with the ideal ordering taken from ALL judged products for the
    query (the pool), so different systems are compared on the same scale.
    """
    gains = [labels.get(pid, 0) for pid in ranked_ids[:k]]
    ideal = sorted(labels.values(), reverse=True)[:k]
    best = dcg(ideal)
    return dcg(gains) / best if best > 0 else 0.0


def recall_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float | None:
    relevant = {pid for pid, label in labels.items() if label >= 1}
    if not relevant:
        return None
    return len(relevant & set(ranked_ids[:k])) / len(relevant)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = max(0, math.ceil(p / 100 * len(ordered)) - 1)
    return ordered[index]


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------
JUDGE_PROMPT = """You judge product search results for an online fashion store.

For the shopper's query, give EACH product a relevance label:
  2 = exactly what the shopper asked for: right item type AND the key attributes
      they mentioned (material, colour, gender, style, occasion, brand).
  1 = partly right: right item type but misses an attribute, or a closely related
      item that could still satisfy the shopper.
  0 = wrong item type or not useful for this query.

Ignore price completely (it is checked separately).
The query may be in any language; judge by its meaning.
Return one label for every product id you are given, with a reason of at most 12 words."""


class JudgeItem(BaseModel):
    id: int = Field(description="The product's id number from the list")
    label: int = Field(ge=0, le=2)
    reason: str = Field(max_length=120)


class JudgeBatch(BaseModel):
    labels: list[JudgeItem]


def build_judge():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SystemExit("GROQ_API_KEY is not set in .env (needed for the LLM judge).")
    from langchain_groq import ChatGroq
    chat = ChatGroq(model=JUDGE_MODEL, api_key=api_key, temperature=0, timeout=60,
                    max_retries=0, reasoning_effort="low")
    return chat.with_structured_output(JudgeBatch)


def judge_products(judge, query: str, products: list[dict]) -> dict[str, dict]:
    """Label products for one query, in chunks. Returns {parent_asin: {label, reason}}."""
    from langchain_core.messages import HumanMessage, SystemMessage
    out: dict[str, dict] = {}
    for start in range(0, len(products), JUDGE_CHUNK):
        chunk = products[start:start + JUDGE_CHUNK]
        listing = "\n\n".join(
            f"[{i}] {p['search_text'][:TEXT_CHARS].replace(chr(10), ' | ')}"
            for i, p in enumerate(chunk)
        )
        messages = [SystemMessage(content=JUDGE_PROMPT),
                    HumanMessage(content=f"Shopper's query: {query}\n\nProducts:\n{listing}")]

        result = None
        for attempt in range(6):
            try:
                result = judge.invoke(messages)
                break
            except Exception as exc:   # rate limits and transient errors: back off
                wait = min(60, 5 * 2 ** attempt)
                print(f"    judge call failed ({type(exc).__name__}); retrying in {wait}s")
                time.sleep(wait)
        if result is None:
            raise RuntimeError("Judge failed repeatedly; try again later or set EVAL_JUDGE_MODEL.")

        by_index = {item.id: item for item in result.labels}
        for i, product in enumerate(chunk):
            item = by_index.get(i)
            if item is None:
                print(f"    judge skipped {product['parent_asin']}; counting it as 0")
                out[product["parent_asin"]] = {"label": 0, "reason": "(not labelled by judge)"}
            else:
                out[product["parent_asin"]] = {"label": item.label, "reason": item.reason}
    return out


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def load_queries(limit: int | None) -> list[dict]:
    with QUERIES_FILE.open(encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    return cases[:limit] if limit else cases


def load_label_cache() -> dict:
    if LABELS_FILE.exists():
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_label_cache(cache: dict) -> None:
    LABELS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def run(args) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cases = load_queries(args.limit)
    label_cache = load_label_cache()          # {query_id: {parent_asin: {label, reason, title}}}
    judge = build_judge()
    http = httpx.Client(timeout=60)

    if not args.keep_cache:
        print(f"Cleared {clear_caches()} cached intents and search results "
              f"(so parse latency is real and searches run the full pipeline).")

    # Phase 1: parse every query BEFORE any judge call. The judge uses the same
    # Groq account and model as the Query Service, so interleaving them made the
    # judge use up the rate limit and /parse fell back to the rules.
    print("Parsing all queries...")
    parsed_by_id = {}
    for n, case in enumerate(cases, start=1):
        if n > 1:
            time.sleep(PARSE_PAUSE_S)
        parsed_by_id[case["id"]] = post(http, f"{QUERY_URL}/parse", {"query": case["query"]})
    fallbacks = sum(1 for p in parsed_by_id.values() if p["source"] != "llm")
    print(f"Parsed {len(cases)} queries; {fallbacks} fell back to the rules.")

    # Phase 2: retrieval, search and judging.
    # Warm-up: the first search after a restart pays model warm-up costs.
    print("Warm-up search...")
    post(http, f"{GATEWAY_URL}/search", {"query": "warm up shirt", "top_n": TOP_N})

    rows = []
    for n, case in enumerate(cases, start=1):
        print(f"[{n}/{len(cases)}] {case['id']}  {case['query']}")

        # 1. Parse (done in phase 1)
        parsed = parsed_by_id[case["id"]]
        checks = intent_checks(case, parsed)

        # 2. Retrieval: hybrid (what the system uses), dense and sparse (pool only)
        base = {"query": parsed["search_query"], "top_k": TOP_K, "max_price": parsed["max_price"]}
        runs = {mode: post(http, f"{RETRIEVAL_URL}/retrieve", {**base, "mode": mode})["candidates"]
                for mode in ("hybrid", "dense", "sparse")}

        # 3. Final results through the gateway (intent is now cached)
        search = post(http, f"{GATEWAY_URL}/search", {"query": case["query"], "top_n": TOP_N})

        # 4. Judge the pool (cached labels are reused)
        pool = {}
        for candidates in runs.values():
            for c in candidates:
                pool.setdefault(c["parent_asin"], c)
        cached = label_cache.setdefault(case["id"], {})
        to_judge = [p for asin, p in pool.items() if asin not in cached]
        if to_judge:
            print(f"    judging {len(to_judge)} products ({len(pool) - len(to_judge)} cached)")
            judged = judge_products(judge, case["query"], to_judge)
            for asin, verdict in judged.items():
                cached[asin] = {**verdict, "title": pool[asin]["title"]}
            save_label_cache(label_cache)
        labels = {asin: cached[asin]["label"] for asin in pool if asin in cached}

        # 5. Metrics for this query
        hybrid_ids = [c["parent_asin"] for c in runs["hybrid"]]
        final_ids = [r["parent_asin"] for r in search["results"]]
        for asin in final_ids:          # rare: a gateway result outside the pool
            labels.setdefault(asin, cached.get(asin, {}).get("label", 0))

        max_price = parsed["max_price"]
        violations = 0
        if max_price is not None:
            violations = sum(1 for r in search["results"] if r["price"] > max_price + 1e-9)
            violations += sum(1 for c in runs["hybrid"] if c["price"] > max_price + 1e-9)

        rows.append({
            "id": case["id"],
            "query": case["query"],
            "search_query": parsed["search_query"],
            "language": parsed["language"],
            "source": parsed["source"],
            "max_price": max_price,
            "price_ok": price_correct(case.get("expected_price"), max_price),
            "intent": checks,
            "parse_ms": parsed["parse_ms"],
            "parse_cache_hit": parsed["cache_hit"],
            "relevant_in_pool": sum(1 for v in labels.values() if v >= 1),
            "ndcg5_retrieval": ndcg_at_k(hybrid_ids, labels, TOP_N),
            "ndcg5_reranked": ndcg_at_k(final_ids, labels, TOP_N),
            "recall20": recall_at_k(hybrid_ids, labels, TOP_K),
            "violations": violations,
            "degraded": search["degraded"],
            "latency_ms": search["latency_ms"],
            "final": [{"parent_asin": r["parent_asin"], "title": r["title"], "price": r["price"],
                       "label": labels.get(r["parent_asin"], 0)} for r in search["results"]],
        })

    http.close()
    write_outputs(rows, label_cache)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def mean(values) -> float:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else float("nan")


def write_outputs(rows: list[dict], label_cache: dict) -> None:
    n = len(rows)
    judged = [r for r in rows if r["relevant_in_pool"] > 0]     # queries the catalogue can answer
    no_match = [r for r in rows if r["relevant_in_pool"] == 0]

    price_acc = sum(r["price_ok"] for r in rows) / n
    intent_acc = sum(r["intent"]["passed"] for r in rows) / n
    llm_rows = [r for r in rows if r["source"] == "llm"]
    llm_passed = sum(r["intent"]["passed"] for r in llm_rows)
    llm_intent_acc = llm_passed / len(llm_rows) if llm_rows else float("nan")
    cold_parse = [r["parse_ms"] for r in rows if not r["parse_cache_hit"] and r["source"] == "llm"]
    totals = [r["latency_ms"]["total_ms"] for r in rows]
    reranks = [r["latency_ms"]["rerank_ms"] for r in rows]
    retrievals = [r["latency_ms"]["retrieval_ms"] for r in rows]

    ndcg_ret = mean(r["ndcg5_retrieval"] for r in judged)
    ndcg_rr = mean(r["ndcg5_reranked"] for r in judged)
    recall = mean(r["recall20"] for r in judged)
    violations = sum(r["violations"] for r in rows)
    degraded = sum(1 for r in rows if r["degraded"])

    lines = [
        "# Evaluation report",
        "",
        f"Queries: {n}  ·  judge model: {JUDGE_MODEL}  ·  "
        f"{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Query parsing",
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Price accuracy | {price_acc:.1%} ({sum(r['price_ok'] for r in rows)}/{n}) |",
        f"| Intent quality (key words + language + no price leak) | {intent_acc:.1%} "
        f"({sum(r['intent']['passed'] for r in rows)}/{n}) |",
        f"| Intent quality, LLM-parsed queries only | {llm_intent_acc:.1%} "
        f"({llm_passed}/{len(llm_rows)}) |",
        f"| Parsed by the LLM (not the rules fallback) | {len(llm_rows)}/{n} |",
        f"| LLM parse latency p50 / p95 | {percentile(cold_parse, 50):.0f} ms / "
        f"{percentile(cold_parse, 95):.0f} ms |",
        "",
        "## Search quality",
        "",
        f"Measured on {len(judged)} queries with at least one relevant product in the pool "
        f"({len(no_match)} had none: a catalogue coverage gap, listed below).",
        "",
        "| Metric | Retrieval order | After rerank |",
        "| --- | --- | --- |",
        f"| nDCG@5 | {ndcg_ret:.3f} | {ndcg_rr:.3f} |",
        f"| Recall@20 (retrieval) | {recall:.3f} | |",
        "",
        f"Rerank lift (nDCG@5): {ndcg_rr - ndcg_ret:+.3f}",
        "",
        "## System",
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Price violations in results | {violations} |",
        f"| Searches with a fallback (degraded) | {degraded}/{n} |",
        f"| Search total latency p50 / p95 (intent cached) | {percentile(totals, 50):.0f} ms / "
        f"{percentile(totals, 95):.0f} ms |",
        f"| Retrieval p50 / Rerank p50 | {percentile(retrievals, 50):.0f} ms / "
        f"{percentile(reranks, 50):.0f} ms |",
        "",
        "## Per query",
        "",
        "| id | query | LLM phrase | price ok | intent ok | nDCG@5 ret → rerank | Recall@20 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        recall_text = f"{r['recall20']:.2f}" if r["recall20"] is not None else "n/a"
        lines.append(
            f"| {r['id']} | {r['query']} | {r['search_query']} | {'yes' if r['price_ok'] else 'NO'} "
            f"| {'yes' if r['intent']['passed'] else 'NO'} "
            f"| {r['ndcg5_retrieval']:.2f} → {r['ndcg5_reranked']:.2f} | {recall_text} |")

    failures = [r for r in rows if not r["intent"]["passed"] or not r["price_ok"]]
    if failures:
        lines += ["", "## Parser failures", ""]
        for r in failures:
            i = r["intent"]
            lines.append(f"- **{r['id']}** `{r['query']}` → `{r['search_query']}` ({r['language']}): "
                         f"missing {i['missing_words'] or '-'}, forbidden {i['forbidden_words'] or '-'}, "
                         f"price leaked {i['price_leaked']}, language ok {i['language_ok']}, "
                         f"price ok {r['price_ok']}")
    if no_match:
        lines += ["", "## No relevant product in the catalogue", ""]
        lines += [f"- {r['id']} `{r['query']}`" for r in no_match]

    report = "\n".join(lines) + "\n"
    (RESULTS_DIR / "report.md").write_text(report, encoding="utf-8")
    with (RESULTS_DIR / "per_query.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_spot_check(label_cache, {r["id"]: r["query"] for r in rows})

    # Short console summary
    print("\n" + "=" * 60)
    print(f"Parser   price accuracy {price_acc:.1%}   intent quality {intent_acc:.1%}"
          f" (LLM-parsed only {llm_intent_acc:.1%}, fallbacks {n - len(llm_rows)}/{n})")
    print(f"Search   nDCG@5 {ndcg_ret:.3f} -> {ndcg_rr:.3f} (rerank lift {ndcg_rr - ndcg_ret:+.3f})"
          f"   Recall@20 {recall:.3f}")
    print(f"System   p50 {percentile(totals, 50):.0f} ms  p95 {percentile(totals, 95):.0f} ms"
          f"   price violations {violations}   degraded {degraded}/{n}")
    print(f"Coverage gaps (no relevant product): {len(no_match)}/{n}")
    print(f"\nFull report: {RESULTS_DIR / 'report.md'}")
    print(f"Spot check:  fill human_label in {RESULTS_DIR / 'spot_check.csv'}, "
          f"then run with --agreement")


def write_spot_check(label_cache: dict, queries: dict[str, str]) -> None:
    """A random sample of judge labels for a human to check. Kept if already started."""
    path = RESULTS_DIR / "spot_check.csv"
    if path.exists():
        return    # don't overwrite labels you may have filled in
    items = [(qid, asin, v) for qid, products in label_cache.items() if qid in queries
             for asin, v in products.items()]
    rng = random.Random(42)
    rng.shuffle(items)
    # Balanced sample: most pool items are 0s, and agreeing on obvious 0s would
    # make the judge look better than it is. Take an equal share of each label.
    per_label = SPOT_CHECK_SIZE // 3
    sample = []
    for label in (2, 1, 0):
        sample += [it for it in items if it[2]["label"] == label][:per_label]
    chosen = {(q, a) for q, a, _ in sample}
    sample += [it for it in items if (it[0], it[1]) not in chosen][:SPOT_CHECK_SIZE - len(sample)]
    rng.shuffle(sample)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["query_id", "query", "parent_asin", "title",
                         "judge_label", "judge_reason", "human_label"])
        for qid, asin, v in sample:
            writer.writerow([qid, queries[qid], asin, v.get("title", ""),
                             v["label"], v["reason"], ""])


def agreement() -> None:
    path = RESULTS_DIR / "spot_check.csv"
    if not path.exists():
        raise SystemExit("No spot_check.csv yet; run the evaluation first.")
    with path.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["human_label"].strip() != ""]
    if not rows:
        raise SystemExit("Fill the human_label column (0, 1 or 2) first.")
    exact = sum(int(r["human_label"]) == int(r["judge_label"]) for r in rows)
    binary = sum((int(r["human_label"]) >= 1) == (int(r["judge_label"]) >= 1) for r in rows)
    print(f"Spot-checked labels:          {len(rows)}")
    print(f"Exact agreement (0/1/2):      {exact}/{len(rows)} = {exact / len(rows):.0%}")
    print(f"Relevant-or-not agreement:    {binary}/{len(rows)} = {binary / len(rows):.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate parsing, retrieval and reranking.")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N queries")
    parser.add_argument("--keep-cache", action="store_true",
                        help="Don't clear the intent cache first (parse latency will look tiny)")
    parser.add_argument("--agreement", action="store_true",
                        help="Report judge-vs-human agreement from spot_check.csv")
    args = parser.parse_args()
    if args.agreement:
        agreement()
    else:
        run(args)


if __name__ == "__main__":
    sys.exit(main())