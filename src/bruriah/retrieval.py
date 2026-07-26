# Snapshot-bound local retrieval: bounded BM25 + vector legs fused with RRF (Slice 6A).
# The candidate schema has no FTS5 table and the snapshot is opened read-only/immutable,
# so lexical scoring is a bounded pure-Python BM25 scan instead of SQLite FTS5.
#
# Budget mapping. A local snapshot scan is bounded by max_elapsed_ms, returned results by
# max_candidates, and returned text by max_extracted_chars. max_bytes is the network transfer
# ceiling (design "Network" boundary) and is deliberately NOT applied here: doing so capped a
# local scan at the alphabetically first ~15% of the corpus while reporting only a degradation.
# The deadline is re-checked inside every scan and scoring loop, not only while reading rows --
# reading is ~0.1 ms of a ~330 ms request, so guarding it alone bounded nothing.
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass

from . import language
from .contracts import Budgets, EvidenceRecord
from .index import ActiveSnapshot

EmbedQuery = Callable[[str], bytes]
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_BM25_K1 = 1.5
_BM25_B = 0.75
_RRF_K = 60

# How much the lexical leg still counts when the query language does not match the corpus.
#
# Not zero: a cross-language question can still carry an identifier, a file name or a proper noun
# that BM25 matches exactly and embeddings blur. At this weight a lexical rank-1 hit contributes
# 0.1/61 against a vector rank-1 hit's 1/61, so the leg can only separate candidates the vector
# leg already ranked together -- a tiebreaker, not a voter. That is the role the measurement says
# it should have when it cannot read the query's language: 17% recall@3 on its own, against 58%.
#
# The sweep (evals/project-memory) reads 1.0 -> 33%, 0.5 -> 50%, 0.25 -> 50%, 0.1 -> 58%, 0 -> 58%.
# Anything at or below 0.25 recovers most of the loss, and the gap between 0.25 and 0.1 is a SINGLE
# question out of twelve -- noise at this sample size, and not the reason for the choice.
_CROSS_LINGUAL_LEXICAL_WEIGHT = 0.1

# Corpus language is decided from a bounded, deterministic sample: passages arrive ordered by ref,
# so the same snapshot yields the same verdict without scanning every byte on every query.
_LANGUAGE_SAMPLE_PASSAGES = 64
_LANGUAGE_SAMPLE_CHARS = 400
_SNIPPET_CHARS = 500
_MAX_QUERY_CHARS = 4096
_CLOCK_EVERY = 64


class RetrievalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RetrievalMatch:
    """`rank`/`lexical_rank`/`vector_rank` are ordinal positions, never confidence scores."""

    ref: str
    document_ref: str
    relative_path: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    snippet: str
    source_hash: str
    rank: int
    lexical_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True)
class RetrievalOutcome:
    matches: tuple[RetrievalMatch, ...]
    degradation: tuple[str, ...]
    warnings: tuple[str, ...]
    candidates_scanned: int
    truncated: bool


@dataclass(frozen=True)
class _Passage:
    ref: str
    document_ref: str
    relative_path: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    text: str
    source_hash: str
    vector: bytes


def _tokenize(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_PATTERN.findall(text.casefold()))


def _expired(position: int, deadline: float, clock: Callable[[], float]) -> bool:
    return position % _CLOCK_EVERY == 0 and clock() >= deadline


def _heading_path(raw: str) -> tuple[str, ...]:
    # A wrong-shaped value must fail typed, never crash and never yield a plausible-looking
    # heading path: heading_path feeds the citation locator, so silent corruption is provenance
    # corruption. `"5"` used to become `("5",)` and `{"a": 1}` used to become `("a",)`.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RetrievalError("corrupt_snapshot_metadata") from error
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise RetrievalError("corrupt_snapshot_metadata")
    return tuple(parsed)


def _scan_passages(
    database: sqlite3.Connection, deadline: float, clock: Callable[[], float]
) -> tuple[list[_Passage], bool]:
    rows = database.execute(
        "SELECT ref, document_ref, relative_path, heading_path, start_line, end_line, "
        "text, source_hash, vector FROM passages ORDER BY ref"
    )
    passages: list[_Passage] = []
    stopped = False
    for position, row in enumerate(rows):
        if _expired(position, deadline, clock):
            stopped = True
            break
        ref, document_ref, relative_path, heading_json, start_line, end_line, text, source_hash, vector = row
        passages.append(
            _Passage(
                ref, document_ref, relative_path, _heading_path(heading_json),
                start_line, end_line, text, source_hash, vector,
            )
        )
    return passages, stopped


def _ranked(scored: list[tuple[float, str]]) -> dict[str, int]:
    # Ties break on ascending `ref`, which is stable across processes and hash seeds.
    ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
    return {ref: rank for rank, (_, ref) in enumerate(ordered, start=1)}


def _bm25_ranks(
    passages: list[_Passage], query_tokens: tuple[str, ...], deadline: float, clock: Callable[[], float]
) -> tuple[dict[str, int] | None, bool]:
    if not query_tokens or not passages:
        return None, False

    tokenized: list[tuple[str, ...]] = []
    stopped = False
    for position, passage in enumerate(passages):
        if _expired(position, deadline, clock):
            stopped = True
            break
        tokenized.append(_tokenize(passage.text))

    lengths = [len(tokens) for tokens in tokenized]
    if not lengths:
        return {}, stopped
    average_length = sum(lengths) / len(lengths)

    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    total_documents = len(tokenized)
    terms = set(query_tokens)
    scored: list[tuple[float, str]] = []
    # The deadline can truncate `tokenized` independently of `passages`, so score only the prefix
    # that was actually tokenized. Zipping the full `passages` under strict= would raise a bare
    # ValueError from the iterator advance, before this loop's own deadline check could run.
    scorable = passages[: len(tokenized)]
    for position, (passage, tokens, length) in enumerate(zip(scorable, tokenized, lengths, strict=True)):
        if _expired(position, deadline, clock):
            stopped = True
            break
        if length == 0 or average_length == 0:
            continue
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            document_count = document_frequency.get(term, 0)
            idf = math.log(1 + (total_documents - document_count + 0.5) / (document_count + 0.5))
            denominator = frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average_length)
            total += idf * (frequency * (_BM25_K1 + 1)) / denominator
        if total > 0:
            scored.append((total, passage.ref))
    return _ranked(scored), stopped


def _floats(blob: bytes) -> array | None:
    values = array("f")
    try:
        values.frombytes(blob)
    except (ValueError, TypeError):
        return None
    return values


def _vector_ranks(
    passages: list[_Passage], query_vector: bytes, deadline: float, clock: Callable[[], float]
) -> tuple[dict[str, int] | None, bool]:
    query = _floats(query_vector)
    if query is None or not len(query):
        return None, False
    query_norm = math.sqrt(sum(value * value for value in query))
    if query_norm == 0:
        return None, False

    dimensions = len(query)
    scored: list[tuple[float, str]] = []
    stopped = False
    for position, passage in enumerate(passages):
        if _expired(position, deadline, clock):
            stopped = True
            break
        # A single corrupt or drifted vector must cost only its own candidate, never the leg.
        candidate = _floats(passage.vector)
        if candidate is None or len(candidate) != dimensions:
            continue
        candidate_norm = math.sqrt(sum(value * value for value in candidate))
        if candidate_norm == 0:
            continue
        dot = sum(a * b for a, b in zip(query, candidate, strict=True))
        scored.append((dot / (query_norm * candidate_norm), passage.ref))
    return _ranked(scored), stopped


def _corpus_language(passages: list[_Passage]) -> str | None:
    """The language a bounded, deterministic sample of the corpus is written in.

    Passages arrive ordered by ref, so the sample is the same for the same snapshot on every query
    and the verdict cannot drift between two identical requests. Reading a prefix of each passage
    rather than all of it keeps this a rounding error against the BM25 scan that follows.
    """
    return language.dominant(
        passage.text[:_LANGUAGE_SAMPLE_CHARS] for passage in passages[:_LANGUAGE_SAMPLE_PASSAGES]
    )


def _fuse(
    lexical_ranks: dict[str, int] | None, vector_ranks: dict[str, int] | None,
    lexical_weight: float = 1.0,
) -> list[tuple[str, int | None, int | None]]:
    """Reciprocal-rank fusion, with the lexical leg's contribution scalable.

    `lexical_weight` exists for one measured reason. Asked in Spanish against an English corpus,
    the vector leg alone reaches 58% recall@3 and the equal-weight fusion reaches 33%: BM25 cannot
    match across languages, so it contributes rank noise that drags correct documents out of the
    top three. Asked in English the same leg is the STRONGER one (83% against 58%), so it cannot
    simply be removed -- only discounted where it is known not to apply.
    """
    lexical_ranks = lexical_ranks or {}
    vector_ranks = vector_ranks or {}
    fused: list[tuple[float, str, int | None, int | None]] = []
    for ref in set(lexical_ranks) | set(vector_ranks):
        lexical_rank, vector_rank = lexical_ranks.get(ref), vector_ranks.get(ref)
        score = (lexical_weight / (_RRF_K + lexical_rank) if lexical_rank is not None else 0.0) + (
            1.0 / (_RRF_K + vector_rank) if vector_rank is not None else 0.0
        )
        fused.append((score, ref, lexical_rank, vector_rank))
    fused.sort(key=lambda item: (-item[0], item[1]))
    return [(ref, lexical_rank, vector_rank) for _, ref, lexical_rank, vector_rank in fused]


def _leg_state(ranks: dict[str, int] | None, leg: str, degradation: list[str]) -> None:
    # `None` means the leg could not run; an empty mapping means it ran and matched nothing.
    # Both must be reported: embedding-dimension drift produces the second and used to be silent.
    if ranks is None:
        degradation.append(f"{leg}_leg_unavailable")
    elif not ranks:
        degradation.append(f"{leg}_leg_no_matches")


def search(
    snapshot: ActiveSnapshot,
    query: str,
    budgets: Budgets = Budgets(),
    *,
    embed_query: EmbedQuery | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RetrievalOutcome:
    """Search the given open, read-only snapshot. Never writes, never raises on a budget ceiling."""
    if not isinstance(query, str) or not query.strip():
        raise RetrievalError("empty_query")
    if len(query) > _MAX_QUERY_CHARS:
        raise RetrievalError("query_too_long")

    deadline = clock() + budgets.max_elapsed_ms / 1000
    degradation: list[str] = []
    try:
        passages, scan_stopped = _scan_passages(snapshot.database, deadline, clock)
    except sqlite3.DatabaseError as error:
        raise RetrievalError("snapshot_unreadable") from error

    lexical_ranks, lexical_stopped = _bm25_ranks(passages, _tokenize(query), deadline, clock)
    _leg_state(lexical_ranks, "lexical", degradation)

    vector_ranks: dict[str, int] | None = None
    vector_stopped = False
    if embed_query is None:
        degradation.append("vector_leg_unavailable")
    else:
        try:
            query_vector = embed_query(query)
        except Exception as error:  # noqa: BLE001 -- embed_query is a caller-supplied untrusted
            query_vector = None     # callable; a failing leg must degrade, never crash the request.
            degradation.append(f"vector_leg_failed:{type(error).__name__}")
        if query_vector is not None:
            vector_ranks, vector_stopped = _vector_ranks(passages, query_vector, deadline, clock)
            _leg_state(vector_ranks, "vector", degradation)

    if scan_stopped or lexical_stopped or vector_stopped:
        degradation.append("max_elapsed_ms_exceeded")

    # Discount the lexical leg when the question is not in the language the corpus is written in.
    # Both legs still run and both ranks are still reported: this changes the weight of evidence,
    # never which evidence exists. Disclosed in `degradation` rather than applied silently, because
    # a caller comparing two result sets is entitled to know the ranking rule was not the same.
    lexical_weight = 1.0
    query_language = language.detect(query)
    corpus_language = _corpus_language(passages)
    if query_language is not None and corpus_language is not None \
            and query_language != corpus_language:
        lexical_weight = _CROSS_LINGUAL_LEXICAL_WEIGHT
        degradation.append(f"lexical_leg_discounted:{query_language}_query_{corpus_language}_corpus")

    by_ref = {passage.ref: passage for passage in passages}
    matches: list[RetrievalMatch] = []
    truncated = False
    extracted = 0
    for rank, (ref, lexical_rank, vector_rank) in enumerate(_fuse(lexical_ranks, vector_ranks, lexical_weight), start=1):
        if len(matches) >= budgets.max_candidates:
            truncated = True
            degradation.append("max_candidates_exceeded")
            break
        if extracted >= budgets.max_extracted_chars:
            truncated = True
            degradation.append("max_extracted_chars_exceeded")
            break
        passage = by_ref[ref]
        snippet = passage.text[: min(_SNIPPET_CHARS, budgets.max_extracted_chars - extracted)]
        extracted += len(snippet)
        matches.append(
            RetrievalMatch(
                ref=passage.ref, document_ref=passage.document_ref, relative_path=passage.relative_path,
                heading_path=passage.heading_path, start_line=passage.start_line, end_line=passage.end_line,
                snippet=snippet, source_hash=passage.source_hash,
                rank=rank, lexical_rank=lexical_rank, vector_rank=vector_rank,
            )
        )

    warnings = ["no_eligible_results"] if not matches else []
    return RetrievalOutcome(
        matches=tuple(matches),
        degradation=tuple(dict.fromkeys(degradation)),
        warnings=tuple(warnings),
        candidates_scanned=len(passages),
        truncated=truncated,
    )


def to_evidence_records(outcome: RetrievalOutcome) -> list[EvidenceRecord]:
    """Show the output is expressible in the closed evidence model; every assessment field is
    this slice's conservative "unknown" -- never inferred from rank."""
    return [
        EvidenceRecord(
            ref=match.ref, kind="local", publisher=match.relative_path, locator=match.relative_path,
            citation_locator=f"{match.relative_path}#{match.start_line}-{match.end_line}",
            digest=f"sha256:{match.source_hash}", extraction_method="markdown_section",
            authority="unknown", authority_rationale="not_assessed_by_retrieval",
            freshness="unknown", license="unknown", conflict="unknown",
        )
        for match in outcome.matches
    ]
