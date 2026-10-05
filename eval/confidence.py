"""
How sure can we be about the rerank lift? Reads eval/results/per_query.jsonl
(no new searches or judge calls) and prints:
  - win / tie / loss: on how many queries rerank beat, matched or lost to retrieval order
  - a 95% bootstrap confidence interval for the nDCG@5 lift

Run from the project root:
    python -m eval.confidence
"""

import json
import random
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results" / "per_query.jsonl"
SAMPLES = 10_000


def main() -> None:
    rows = [json.loads(line) for line in RESULTS.open(encoding="utf-8")]
    rows = [r for r in rows if r["relevant_in_pool"] > 0]       # same queries as the report
    diffs = [r["ndcg5_reranked"] - r["ndcg5_retrieval"] for r in rows]

    wins = sum(d > 0.001 for d in diffs)
    losses = sum(d < -0.001 for d in diffs)
    ties = len(diffs) - wins - losses
    mean = sum(diffs) / len(diffs)

    # Bootstrap: resample the queries many times and see how much the mean lift moves.
    rng = random.Random(0)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(SAMPLES))
    low, high = means[int(0.025 * SAMPLES)], means[int(0.975 * SAMPLES)]

    print(f"Queries compared:        {len(diffs)}")
    print(f"Rerank better / same / worse: {wins} / {ties} / {losses}")
    print(f"Mean nDCG@5 lift:        {mean:+.3f}")
    print(f"95% confidence interval: [{low:+.3f}, {high:+.3f}]")
    print("Lift is reliably positive." if low > 0 else
          "Interval includes 0: more queries needed to be sure.")


if __name__ == "__main__":
    main()