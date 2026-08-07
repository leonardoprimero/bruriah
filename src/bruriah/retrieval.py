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
# A reranker scores whole documents against the query and returns one number each, higher first.
# Supplied by the caller exactly as `embed_query` is, for the same reason: the model, its download
# and its licence are the operator's choice, and retrieval must stay importable without either.
Rerank = Callable[[str, list[str]], list[float]]
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

# How many DOCUMENTS a supplied reranker is asked to score, and how much of each it reads.
#
# Measured on the two foreign corpora, not guessed. On `square/leakcanary` the correct document is
# somewhere in the returned pool 75.8% of the time while recall@3 is 0.340 -- almost everything
# that is lost is lost ORDERING, not retrieving, and recall@10 understates that headroom by half.
# Reranking the top 40 documents reads 0.431 recall@3 and 0.516 recall@10; the top 20 reads 0.412
# and 0.464. Depth is the whole cost of the stage: one cross-encoder pass per extra document.
#
# 40 is NOT the depth that maximises every corpus, and this constant should not be read as tuned.
# On `emilk/egui` reranking loses to the shipped ranking (0.494 against 0.530) and losing DEEPER
# loses MORE -- top 20 reads 0.518 there. Depth amplifies whichever direction the reranker has on
# a given corpus rather than improving it, so a larger number here would not be safer. The value
# is set where the corpora that gain, gain most, and the whole stage is opt-in precisely because
# whether a corpus gains at all is not predictable from anything measured.
#
# DOCUMENTS, not passages, and that is the larger of the two findings. A passage here is ~250
# characters of a commit body whose median length is 445, and scoring passages directly reaches
# only 0.373 with a 1.11 GB model -- the same figure an 0.08 GB model reaches when it is handed
# whole documents. The unit fed to the cross-encoder matters more than the size of the model.
_RERANK_DEPTH = 40
_RERANK_MAX_CHARS = 4000
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


def _document_text(passages: list[_Passage], fused_position: dict[str, int]) -> str:
    """One document rebuilt from the passages already scanned, MOST RELEVANT PASSAGE FIRST.

    The snapshot's `documents` table carries metadata and no text, so this is the only document
    text `search` can offer a reranker without a second read of the corpus -- which it cannot do
    anyway, being snapshot-bound. It was VALIDATED as the input rather than assumed: scoring text
    rebuilt this way reproduces the figure measured against the corpus files themselves, so the
    number in `_RERANK_DEPTH`'s note belongs to the pipeline that actually ships.

    Relevance order, not file order, because the result is truncated at `_RERANK_MAX_CHARS` and
    file order decides what survives that cut by where an author happened to put it. Measured on
    `emilk/egui`: one 14,071-character commit is the recorded answer to three separate questions,
    and the shipped ranking put it FIRST for all three. Its opening 4,000 characters are the
    repository's pull-request template -- "Keep your PR:s small and focused" -- so the cross-encoder
    was asked whether a CONTRIBUTING.md checklist answered a question about IME composition,
    correctly said no, and dropped the answer to ranks 18, 30 and 35. The text it needed began at
    character 4,100.

    Ordering by the position `_fuse` already gave each passage costs nothing and needs no second
    model: the ranking has ALREADY decided which passages match this query. A passage the fusion
    never ranked -- possible when the vector leg is unavailable and BM25 matched nothing in it --
    sorts after every ranked one, in file order, so a document still arrives whole. That is the
    property this function must not lose: whole documents beat passages by a wide measured margin,
    and reordering them is not the same as splitting them.
    """
    ordered = sorted(
        passages,
        key=lambda item: (fused_position.get(item.ref, len(fused_position)), item.start_line),
    )
    return "\n\n".join(passage.text for passage in ordered)


def _rerank_fused(
    fused: list[tuple[str, int | None, int | None]],
    passages: list[_Passage],
    by_ref: dict[str, _Passage],
    query: str,
    rerank: Rerank,
    deadline: float,
    clock: Callable[[], float],
    degradation: list[str],
) -> list[tuple[str, int | None, int | None]]:
    """Reorder the head of the fused list by a cross-encoder's reading of whole documents.

    Reranking changes the ORDER of the evidence and never its membership: every ref returned here
    was returned by `_fuse`, and every passage of a document travels with it. That is the rule the
    cross-lingual discount already follows, for the same reason -- a ranking rule may reweigh
    evidence, but inventing or dropping it would leave `lexical_rank`/`vector_rank` describing a
    list that no longer exists.

    Every failure path returns the fused order untouched and says so. A reranker is a caller-
    supplied model that can be slow, absent or wrong, and the shipped ranking without it is a
    measured 0.340 rather than nothing: degrading to it is a real answer, not an error.
    """
    if not fused:
        return fused
    if clock() >= deadline:
        degradation.append("max_elapsed_ms_exceeded")
        return fused

    within: dict[str, list[tuple[str, int | None, int | None]]] = {}
    for entry in fused:
        within.setdefault(by_ref[entry[0]].document_ref, []).append(entry)
    order = list(within)  # Insertion order is fused order: dicts preserve it, first passage wins.
    head, tail = order[:_RERANK_DEPTH], order[_RERANK_DEPTH:]

    grouped: dict[str, list[_Passage]] = {}
    for passage in passages:
        grouped.setdefault(passage.document_ref, []).append(passage)
    # What `_fuse` already decided about every passage, reused so the truncation below keeps the
    # part of a long document that matched rather than the part that came first in the file.
    fused_position = {entry[0]: index for index, entry in enumerate(fused)}

    try:
        returned = list(rerank(
            query,
            [_document_text(grouped.get(document_ref, []), fused_position)[:_RERANK_MAX_CHARS]
             for document_ref in head],
        ))
    except Exception as error:  # noqa: BLE001 -- caller-supplied untrusted callable, as embed_query
        degradation.append(f"rerank_failed:{type(error).__name__}")
        return fused
    if len(returned) != len(head):
        degradation.append("rerank_failed:score_count_mismatch")
        return fused
    try:
        scores = [float(score) for score in returned]
    except (TypeError, ValueError):
        degradation.append("rerank_failed:non_numeric_score")
        return fused
    if any(score != score for score in scores):  # NaN sorts unpredictably and would be silent.
        degradation.append("rerank_failed:non_numeric_score")
        return fused

    # Ties break on the position the document already held, so a reranker that scores two documents
    # identically cannot reorder them and two identical requests cannot disagree.
    ranked = [
        document_ref for _score, _position, document_ref in sorted(
            ((score, position, document_ref)
             for position, (score, document_ref) in enumerate(zip(scores, head, strict=True))),
            key=lambda item: (-item[0], item[1]),
        )
    ]
    degradation.append(f"reranked:{len(head)}_documents")
    # Interleaved, NOT concatenated by document, and that is a budget decision rather than an
    # ordering one. Both forms place the DOCUMENTS in exactly the ranking above -- a document's
    # first passage sits at the same position either way -- so the document-level ranking every
    # published figure is measured against is identical. What differs is which passages survive
    # `max_candidates`, which counts passages.
    #
    # Concatenation spends that budget on whichever documents happen to hold many passages.
    # Measured with a reranker returning one constant score for every document (a no-op by this
    # docstring and by the tie-break note below): it changed the returned set on 100% of the 236
    # foreign-corpus questions, halved the distinct documents returned at the default budget
    # (48.4 -> 24.3 on leakcanary, 46.1 -> 17.8 on egui) and dropped the recorded answer out of
    # the pool for 21 of them.
    #
    # Passages of one document are therefore NOT contiguous in the result. That was asserted here
    # until 2026-08-06, and the assertion had no measurement behind it: the measured finding about
    # whole documents is about what the cross-encoder is FED (`_document_text`), which is unchanged
    # and still covered by its own test.
    #
    # After the change, on the same 236 questions: distinct documents returned at the default
    # budget went 24.3 -> 49.5 (leakcanary) and 17.8 -> 50.0 (egui), ABOVE the 48.4 and 46.1 the
    # same searches return with no reranker at all, since one passage per document is the most
    # breadth a passage budget can buy. All 21 dropped answers came back, and the document-level
    # rank of every answer already present moved for ZERO questions -- which is what makes this
    # free: every recall figure this project publishes is measured over deduped documents.
    order = ranked + tail
    depth = max((len(within[document_ref]) for document_ref in order), default=0)
    return [
        within[document_ref][index]
        for index in range(depth)
        for document_ref in order
        if index < len(within[document_ref])
    ]


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
    rerank: Rerank | None = None,
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
    ordered = _fuse(lexical_ranks, vector_ranks, lexical_weight)
    # Absence of a reranker is deliberately NOT reported the way `vector_leg_unavailable` is. The
    # vector leg is part of the shipped ranking and its absence is a shortfall; a reranker is an
    # opt-in stage that is off by default, so announcing it on every request would add a line to
    # every existing response to say that nothing happened.
    if rerank is not None:
        ordered = _rerank_fused(
            ordered, passages, by_ref, query, rerank, deadline, clock, degradation
        )

    matches: list[RetrievalMatch] = []
    truncated = False
    extracted = 0
    for rank, (ref, lexical_rank, vector_rank) in enumerate(ordered, start=1):
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
