# Snapshot-bound local retrieval: bounded BM25 + vector legs fused with RRF (Slice 6A).
# The candidate schema has no FTS5 table and the snapshot is opened read-only/immutable,
# so lexical scoring is a bounded pure-Python BM25 scan instead of SQLite FTS5.
from __future__ import annotations

from . import ranking

_BM25_K1 = ranking.BM25_K1
_BM25_B = ranking.BM25_B
_RRF_K = ranking.RRF_K

# Lexical discount weight for cross-lingual queries (when query lang != corpus lang).
# Keeps lexical leg as a tiebreaker for exact symbols/paths (1/10th of vector weight).
_CROSS_LINGUAL_LEXICAL_WEIGHT = ranking.CROSS_LINGUAL_LEXICAL_WEIGHT

_fuse = ranking.fuse_ranks


def fused_order(
    lexical_ranks: dict[str, int] | None,
    vector_ranks: dict[str, int] | None,
    lexical_weight: float,
) -> list[tuple[str, int | None, int | None]]:
    """The fused order `SearchService.search` pages through."""
    return _fuse(lexical_ranks, vector_ranks, lexical_weight)
