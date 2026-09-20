"""Pure ranking algorithms: BM25 scoring, vector similarity, and Reciprocal Rank Fusion (RRF).

All ranking and scoring logic in this module is deterministic, in-memory, and free of I/O or
database dependencies.
"""
from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Iterable, Sequence

BM25_K1: float = 1.5
BM25_B: float = 0.75
# Tuned from the literature default of 60. Lower K gives stronger rank advantage to top positions;
# at 60, a document at rank 1 scores 0.0164 vs 0.0156 at rank 2 -- nearly indistinguishable.
# At K=20, rank 1 scores 0.0476 vs 0.0455 at rank 2 -- a 4.6% gap per leg, doubled in fusion.
# This matters when the vector leg already places the correct document at rank 1-5 but RRF
# fails to elevate it into the top-3 window.
# Measured: run evals/retrieval/run_ablation.py --rrf-k 20 to verify against your corpus.
RRF_K: int = 20
CROSS_LINGUAL_LEXICAL_WEIGHT: float = 0.1
RERANK_DEPTH: int = 40
RERANK_MAX_CHARS: int = 4000
CLOCK_EVERY: int = 64


def default_expired(position: int, deadline: float, clock: Callable[[], float]) -> bool:
    """Check if the execution budget has expired at periodic position intervals."""
    return position % CLOCK_EVERY == 0 and clock() >= deadline


def ranked(scored: list[tuple[float, str]]) -> dict[str, int]:
    """Assign stable 1-based ranks to scored items.

    Ties break on ascending `ref`, which is stable across processes and hash seeds.
    """
    ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
    return {ref: rank for rank, (_, ref) in enumerate(ordered, start=1)}


def floats(blob: bytes) -> array | None:
    """Decode a bytes blob into an array of 32-bit floats."""
    values = array("f")
    try:
        values.frombytes(blob)
    except (ValueError, TypeError):
        return None
    return values


def bm25_scores_from_tokens(
    tokenized: Sequence[tuple[str, ...]],
    refs: Sequence[str],
    query_tokens: tuple[str, ...],
    deadline: float,
    clock: Callable[[], float],
    is_expired: Callable[[int, float, Callable[[], float]], bool] = default_expired,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> tuple[dict[str, int] | None, bool]:
    """Compute BM25 ranks over pre-tokenized passages against query tokens."""
    if not query_tokens or not tokenized or not refs:
        return None, False

    lengths = [len(tokens) for tokens in tokenized]
    if not lengths:
        return {}, False
    average_length = sum(lengths) / len(lengths)

    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    total_documents = len(tokenized)
    terms = set(query_tokens)
    idfs = {
        term: math.log(
            1 + (total_documents - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
        )
        for term in terms
        if term in document_frequency
    }
    k1_plus_1 = k1 + 1
    b_part = 1 - b
    b_over_avg = b / average_length if average_length > 0 else 0.0
    scored: list[tuple[float, str]] = []

    scorable_refs = refs[: len(tokenized)]
    stopped = False
    for position, (ref, tokens, length) in enumerate(zip(scorable_refs, tokenized, lengths, strict=True)):
        if is_expired(position, deadline, clock):
            stopped = True
            break
        if length == 0 or average_length == 0:
            continue
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = 0.0
        len_factor = k1 * (b_part + length * b_over_avg)
        for term, idf in idfs.items():
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            denominator = frequency + len_factor
            total += idf * (frequency * k1_plus_1) / denominator
        if total > 0:
            scored.append((total, ref))
    return ranked(scored), stopped


def bm25_scores_from_postings(
    total_documents: int,
    average_length: float,
    dfs: dict[str, int],
    postings: Iterable[tuple[str, str, int, int]],  # (term, ref, freq, doc_length)
    deadline: float,
    clock: Callable[[], float],
    is_expired: Callable[[int, float, Callable[[], float]], bool] = default_expired,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> tuple[dict[str, int] | None, bool]:
    """Compute BM25 ranks from corpus statistics and posting records."""
    if total_documents == 0:
        return None, False
    if average_length == 0.0 or not dfs:
        return {}, False

    idfs = {
        term: math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
        for term, df in dfs.items()
    }

    k1_plus_1 = k1 + 1
    b_part = 1 - b
    b_over_avg = b / average_length

    scores: dict[str, float] = {}
    stopped = False
    for position, (term, ref, freq, doc_length) in enumerate(postings):
        if is_expired(position, deadline, clock):
            stopped = True
            break
        if doc_length == 0:
            continue
        idf = idfs.get(term)
        if idf is None:
            continue
        len_factor = k1 * (b_part + doc_length * b_over_avg)
        denominator = freq + len_factor
        scores[ref] = scores.get(ref, 0.0) + idf * (freq * k1_plus_1) / denominator

    scored = [(score, ref) for ref, score in scores.items() if score > 0]
    return ranked(scored), stopped


def vector_ranks(
    items: Sequence[tuple[str, bytes]],
    query_vector: bytes,
    deadline: float,
    clock: Callable[[], float],
    is_expired: Callable[[int, float, Callable[[], float]], bool] = default_expired,
) -> tuple[dict[str, int] | None, bool]:
    """Compute cosine-similarity ranks for candidate vectors against a query vector."""
    query = floats(query_vector)
    if query is None or not len(query):
        return None, False
    query_norm = math.sqrt(sum(value * value for value in query))
    if query_norm == 0:
        return None, False

    inv_query_norm = 1.0 / query_norm
    norm_query = tuple(value * inv_query_norm for value in query)
    dimensions = len(norm_query)
    scored: list[tuple[float, str]] = []
    stopped = False
    for position, (ref, vec) in enumerate(items):
        if is_expired(position, deadline, clock):
            stopped = True
            break
        candidate = floats(vec)
        if candidate is None or len(candidate) != dimensions:
            continue
        dot = 0.0
        candidate_sum_sq = 0.0
        for q_val, c_val in zip(norm_query, candidate, strict=True):
            dot += q_val * c_val
            candidate_sum_sq += c_val * c_val
        if candidate_sum_sq == 0.0:
            continue
        scored.append((dot / math.sqrt(candidate_sum_sq), ref))
    return ranked(scored), stopped


def fuse_ranks(
    lexical_ranks: dict[str, int] | None,
    vector_ranks: dict[str, int] | None,
    lexical_weight: float = 1.0,
    rrf_k: int = RRF_K,
) -> list[tuple[str, int | None, int | None]]:
    """Reciprocal-rank fusion, with the lexical leg's contribution scalable.

    `rrf_k` controls rank-decay strength and defaults to the module constant `RRF_K` (currently 20,
    tuned down from the literature default of 60). Pass an explicit `rrf_k` to override per-call
    without changing the global default -- useful in ablation sweeps (e.g. run_ablation.py)."""
    lexical_ranks = lexical_ranks or {}
    vector_ranks = vector_ranks or {}
    fused: list[tuple[float, str, int | None, int | None]] = []
    for ref in set(lexical_ranks) | set(vector_ranks):
        lexical_rank = lexical_ranks.get(ref)
        vector_rank = vector_ranks.get(ref)
        score = (
            (lexical_weight / (rrf_k + lexical_rank) if lexical_rank is not None else 0.0)
            + (1.0 / (rrf_k + vector_rank) if vector_rank is not None else 0.0)
        )
        fused.append((score, ref, lexical_rank, vector_rank))
    fused.sort(key=lambda item: (-item[0], item[1]))
    return [(ref, lexical_rank, vector_rank) for _, ref, lexical_rank, vector_rank in fused]
