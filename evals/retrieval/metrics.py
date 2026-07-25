"""Pure, unit-testable retrieval-quality metrics for the Cerebro new-vs-legacy eval.

Every function here operates on NOTE-LEVEL (already deduped) ranked lists and
engine-independent ground truth -- never on raw chunk/section results, and
never on "what an engine returned" as a definition of correctness. See
`run_eval.py` for how these are wired against the two live engines.

No wall-clock, no randomness, no I/O: every function is a pure computation
over its arguments, so the same inputs always produce the same outputs.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Core rank/graded metrics
# ---------------------------------------------------------------------------


def recall_at_k(ranked_notes: Sequence[str], must_include: Sequence[str], k: int) -> float | None:
    """Fraction of `must_include` notes present in the top `k` of `ranked_notes`.

    Returns `None` (never `0.0`) when `must_include` is empty: an abstention
    query has no positive ground truth, so recall is undefined for it rather
    than trivially zero -- callers must exclude `None` from aggregates
    instead of averaging it in as a miss.
    """
    if not must_include:
        return None
    top_k = set(ranked_notes[:k])
    hits = sum(1 for note in must_include if note in top_k)
    return hits / len(must_include)


def mrr_at_10(ranked_notes: Sequence[str], must_include: Sequence[str]) -> float | None:
    """Reciprocal rank (1-indexed) of the first `must_include` note within the
    top 10 of `ranked_notes`.

    `0.0` if none of `must_include` appears in the top 10 (a genuine miss).
    `None` if `must_include` is empty (undefined, not a miss -- see
    `recall_at_k`).
    """
    if not must_include:
        return None
    target = set(must_include)
    for rank, note in enumerate(ranked_notes[:10], start=1):
        if note in target:
            return 1.0 / rank
    return 0.0


def exact_name_recall_at_3(ranked_notes: Sequence[str], must_include: Sequence[str]) -> float | None:
    """Stricter k=3 recall, for `exact_name`-type queries where the correct
    note should be found near the very top or not at all."""
    return recall_at_k(ranked_notes, must_include, k=3)


def _relevance(note: str, must_include: Sequence[str], acceptable_only: Sequence[str]) -> int:
    if note in must_include:
        return 2
    if note in acceptable_only:
        return 1
    return 0


def ndcg_at_10(
    ranked_notes: Sequence[str], must_include: Sequence[str], acceptable: Sequence[str]
) -> float | None:
    """Graded nDCG@10 over the top 10 of `ranked_notes`.

    Relevance: 2 for a `must_include` note, 1 for an `acceptable` note not
    already in `must_include`, 0 otherwise. IDCG comes from the ideal
    ordering of every ground-truth-relevant note, capped at 10 -- so a query
    with only one `must_include` note has an ideal DCG of exactly that one
    hit at rank 1, not an inflated ceiling from padding.

    `None` when there is no ground truth at all (abstention queries): nDCG
    is undefined without at least one relevant note, never trivially 0 or 1.
    """
    acceptable_only = [note for note in acceptable if note not in must_include]
    if not must_include and not acceptable_only:
        return None
    dcg = 0.0
    for position, note in enumerate(ranked_notes[:10], start=1):
        relevance = _relevance(note, must_include, acceptable_only)
        if relevance:
            dcg += relevance / math.log2(position + 1)
    ideal_relevances = sorted([2] * len(must_include) + [1] * len(acceptable_only), reverse=True)[:10]
    idcg = sum(rel / math.log2(pos + 1) for pos, rel in enumerate(ideal_relevances, start=1))
    if idcg == 0.0:
        return None
    return dcg / idcg


# ---------------------------------------------------------------------------
# Slice-specific helpers
# ---------------------------------------------------------------------------


def cross_lingual_recall_delta(
    cross_lingual_recalls: Sequence[float], same_language_recalls: Sequence[float]
) -> float | None:
    """Mean recall@10 on `cross_lingual`-type queries minus mean recall@10 on
    the same-language baseline query set, for one engine.

    Negative means cross-lingual queries regress relative to same-language
    queries (the smoke-tested failure mode); positive means the engine holds
    up (or improves) across the language boundary. `None` if either side has
    no scored queries.
    """
    if not cross_lingual_recalls or not same_language_recalls:
        return None
    cross_mean = sum(cross_lingual_recalls) / len(cross_lingual_recalls)
    same_mean = sum(same_language_recalls) / len(same_language_recalls)
    return cross_mean - same_mean


def ambiguous_diversity(ranked_notes: Sequence[str], k: int = 10) -> int:
    """Count of distinct notes in the top `k` of an ambiguous query's ranked
    results. A broad query (e.g. a single word) that repeats the same note
    across ranks is less useful than one that surfaces a diverse top-k."""
    return len(set(ranked_notes[:k]))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    midpoint = n // 2
    if n % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default `linear` method),
    implemented without a numpy dependency."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(frozen=True)
class AbstentionSeparation:
    """Per-engine summary of how well top-1 confidence scores separate
    abstention queries (no valid answer in the corpus) from positive queries
    (a real answer exists). A good engine assigns abstention queries a
    visibly lower top-1 score than positive queries."""

    abstention_median_top1: float | None
    positive_median_top1: float | None
    positive_p25_top1: float | None
    below_p25_fraction: float | None
    abstention_count: int
    positive_count: int


def abstention_score_separation(
    abstention_top1_scores: Sequence[float], positive_top1_scores: Sequence[float]
) -> AbstentionSeparation:
    """Compare the abstention-query and positive-query top-1 score
    distributions for one engine: their medians, the 25th percentile of the
    positive distribution, and the fraction of abstention queries whose
    top-1 score falls below that 25th percentile (higher = better
    separation). Fields are `None` when either side has no scored queries.
    """
    abstention_count = len(abstention_top1_scores)
    positive_count = len(positive_top1_scores)
    if not abstention_count or not positive_count:
        return AbstentionSeparation(None, None, None, None, abstention_count, positive_count)
    positive_p25 = _percentile(positive_top1_scores, 25)
    below = sum(1 for score in abstention_top1_scores if score < positive_p25)
    return AbstentionSeparation(
        abstention_median_top1=_median(abstention_top1_scores),
        positive_median_top1=_median(positive_top1_scores),
        positive_p25_top1=positive_p25,
        below_p25_fraction=below / abstention_count,
        abstention_count=abstention_count,
        positive_count=positive_count,
    )


def mean_or_none(values: Sequence[float | None]) -> float | None:
    """Mean of the non-`None` values in `values`. `None` if every value is
    `None` (no scoreable queries in the slice)."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


__all__ = [
    "AbstentionSeparation",
    "abstention_score_separation",
    "ambiguous_diversity",
    "cross_lingual_recall_delta",
    "exact_name_recall_at_3",
    "mean_or_none",
    "mrr_at_10",
    "ndcg_at_10",
    "recall_at_k",
]
