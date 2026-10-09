"""Pure ranking algorithms: BM25 scoring, vector similarity, and Reciprocal Rank Fusion (RRF).

All ranking and scoring logic in this module is deterministic, in-memory, and free of I/O or
database dependencies.
"""

from __future__ import annotations

BM25_K1: float = 1.5
BM25_B: float = 0.75
# At K=60, a document at rank 1 scores 1/61=0.0164 vs 1/65=0.0154 at rank 5 -- barely
# distinguishable. At K=20, rank 1 scores 1/21=0.0476 vs 1/25=0.0400 at rank 5, so a passage
# either leg places first dominates the fused order instead of tying with the rest of the top five.
RRF_K: int = 20
CROSS_LINGUAL_LEXICAL_WEIGHT: float = 0.1
RERANK_DEPTH: int = 40
RERANK_MAX_CHARS: int = 4000
CLOCK_EVERY: int = 64


def ranked(scored: list[tuple[float, str]]) -> dict[str, int]:
    """Assign stable 1-based ranks to scored items.

    Ties break on ascending `ref`, which is stable across processes and hash seeds.
    """
    ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
    return {ref: rank for rank, (_, ref) in enumerate(ordered, start=1)}


def fuse_ranks(
    lexical_ranks: dict[str, int] | None,
    vector_ranks: dict[str, int] | None,
    lexical_weight: float = 1.0,
    rrf_k: int = RRF_K,
) -> list[tuple[str, int | None, int | None]]:
    """Reciprocal-rank fusion, with the lexical leg's contribution scalable.

    `rrf_k` controls rank-decay strength and defaults to the module constant `RRF_K`.
    Pass an explicit `rrf_k` to override per-call without changing the global default -- useful in
    ablation sweeps (e.g. run_ablation.py)."""
    lexical_ranks = lexical_ranks or {}
    vector_ranks = vector_ranks or {}
    fused: list[tuple[float, str, int | None, int | None]] = []
    for ref in set(lexical_ranks) | set(vector_ranks):
        lexical_rank = lexical_ranks.get(ref)
        vector_rank = vector_ranks.get(ref)
        score = (lexical_weight / (rrf_k + lexical_rank) if lexical_rank is not None else 0.0) + (
            1.0 / (rrf_k + vector_rank) if vector_rank is not None else 0.0
        )
        fused.append((score, ref, lexical_rank, vector_rank))
    fused.sort(key=lambda item: (-item[0], item[1]))
    return [(ref, lexical_rank, vector_rank) for _, ref, lexical_rank, vector_rank in fused]
