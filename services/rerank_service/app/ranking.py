"""
Pure ranking logic (no model): rating blend + MMR.

final_score = relevance + rating_weight * normalized_bayesian_score

MMR (Maximal Marginal Relevance) picks results one at a time. Each pick
maximises:
    lambda * final_score  -  (1 - lambda) * max_similarity_to_already_picked
so a product that is almost identical to one already chosen gets penalised.
"""

import numpy as np


def normalize_ratings(bayesian_scores: list[float]) -> list[float]:
    """Min-max scale ratings to 0..1 within this candidate set."""
    low, high = min(bayesian_scores), max(bayesian_scores)
    if high - low < 1e-9:
        return [0.5] * len(bayesian_scores)
    return [(b - low) / (high - low) for b in bayesian_scores]


def blend(relevance: list[float], bayesian_scores: list[float], rating_weight: float) -> list[float]:
    ratings = normalize_ratings(bayesian_scores)
    return [r + rating_weight * n for r, n in zip(relevance, ratings)]


def cosine_matrix(vectors: list[list[float]]) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.clip(norms, 1e-12, None)
    return matrix @ matrix.T


def mmr(scores: list[float], vectors: list[list[float]] | None, top_n: int, lambda_: float) -> list[int]:
    """
    Return the indices of the chosen candidates, in order.
    Without vectors, it falls back to plain score order (no diversity).
    """
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    if vectors is None or lambda_ >= 1.0:
        return order[:top_n]

    similarity = cosine_matrix(vectors)
    chosen: list[int] = []
    remaining = order[:]
    while remaining and len(chosen) < top_n:
        def mmr_value(i: int) -> float:
            redundancy = max(similarity[i, j] for j in chosen) if chosen else 0.0
            return lambda_ * scores[i] - (1 - lambda_) * redundancy
        best = max(remaining, key=mmr_value)
        chosen.append(best)
        remaining.remove(best)
    return chosen