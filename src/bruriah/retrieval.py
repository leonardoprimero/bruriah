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

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from typing import Literal

from . import language, ranking
from .contracts import AuthorityRationale, Budgets, EvidenceRecord
from .index import ActiveSnapshot
from .repository import PassageRecord, RepositoryError, SnapshotRepository, parse_heading_path

# T2 (investigate-boundary-v2): the fixed literal every local-corpus EvidenceRecord reports as
# its publisher. Never the author-chosen relative path -- that channel is closed; `publisher`
# says WHERE the record comes from in the abstract (this installation's local corpus), which is
# never author-controlled, unlike a file name or a commit-derived slug.
LOCAL_EVIDENCE_PUBLISHER = "local-corpus"

EmbedQuery = Callable[[str], bytes]
# A reranker scores whole documents against the query and returns one number each, higher first.
# Supplied by the caller exactly as `embed_query` is, for the same reason: the model, its download
# and its licence are the operator's choice, and retrieval must stay importable without either.
Rerank = Callable[[str, list[str]], list[float]]
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_BM25_K1 = ranking.BM25_K1
_BM25_B = ranking.BM25_B
_RRF_K = ranking.RRF_K

# Lexical discount weight for cross-lingual queries (when query lang != corpus lang).
# Keeps lexical leg as a tiebreaker for exact symbols/paths (1/10th of vector weight).
# See evals/project-memory for weight sweep: 1.0 -> 33%, 0.1 -> 58% recall@3.
_CROSS_LINGUAL_LEXICAL_WEIGHT = ranking.CROSS_LINGUAL_LEXICAL_WEIGHT

# Corpus language is decided from a bounded, deterministic sample: passages arrive ordered by ref,
# so the same snapshot yields the same verdict without scanning every byte on every query.
_LANGUAGE_SAMPLE_PASSAGES = 64
_LANGUAGE_SAMPLE_CHARS = 400

# Number of documents scored by the optional reranker and character budget per document.
# Documents (not raw passages) are passed to maximize cross-encoder context (evals/project-memory).
# Optimal depth varies by corpus (depth 20 on egui, depth 40 on leakcanary); default is 40.
# See evals/retrieval/run_ablation.py --depth to benchmark on your corpus.
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


_Passage = PassageRecord


def _tokenize(text: str) -> tuple[str, ...]:
    return language.tokenize(text)


def _expired(position: int, deadline: float, clock: Callable[[], float]) -> bool:
    return position % _CLOCK_EVERY == 0 and clock() >= deadline


def _heading_path(raw: str) -> tuple[str, ...]:
    try:
        return parse_heading_path(raw)
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


def _scan_passages(
    database: sqlite3.Connection, deadline: float, clock: Callable[[], float]
) -> tuple[list[_Passage], bool]:
    try:
        return SnapshotRepository(database).scan_passages(deadline, clock, _expired)
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


def _scan_vectors(
    database: sqlite3.Connection, deadline: float, clock: Callable[[], float]
) -> tuple[list[tuple[str, bytes]], bool]:
    try:
        return SnapshotRepository(database).scan_vectors(deadline, clock, _expired)
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


def _hydrate_passages(
    database: sqlite3.Connection, refs: Sequence[str]
) -> dict[str, _Passage]:
    try:
        return SnapshotRepository(database).hydrate_passages(refs)
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


_ranked = ranking.ranked


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
        # `search_text`, so a section is reachable by the headings it sits under. BM25 can only
        # score terms it was handed, and a child section does not contain its parents' words.
        tokenized.append(_tokenize(passage.search_text))

    refs = [passage.ref for passage in passages[: len(tokenized)]]
    ranks, score_stopped = ranking.bm25_scores_from_tokens(
        tokenized=tokenized,
        refs=refs,
        query_tokens=query_tokens,
        deadline=deadline,
        clock=clock,
        is_expired=_expired,
        k1=_BM25_K1,
        b=_BM25_B,
    )
    return ranks, stopped or score_stopped


def _has_lexical_index(database: sqlite3.Connection) -> bool:
    return SnapshotRepository(database).has_lexical_index()


def _bm25_indexed_ranks(
    database: sqlite3.Connection,
    query_tokens: tuple[str, ...],
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, int] | None, bool]:
    if not query_tokens:
        return None, False
    if clock() >= deadline:
        return {}, True

    repo = SnapshotRepository(database)
    try:
        total_documents, average_length = repo.get_corpus_stats()
        if total_documents == 0:
            return None, False
        if average_length == 0.0:
            return {}, False

        dfs = repo.get_term_dfs(query_tokens)
        if not dfs:
            return {}, False

        postings = repo.iter_term_postings(dfs.keys())
        return ranking.bm25_scores_from_postings(
            total_documents=total_documents,
            average_length=average_length,
            dfs=dfs,
            postings=postings,
            deadline=deadline,
            clock=clock,
            is_expired=_expired,
            k1=_BM25_K1,
            b=_BM25_B,
        )
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


_floats = ranking.floats


def _vector_ranks(
    passages: Sequence[_Passage | tuple[str, bytes]],
    query_vector: bytes,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, int] | None, bool]:
    items: list[tuple[str, bytes]] = [
        (item.ref, item.vector) if isinstance(item, _Passage) else item
        for item in passages
    ]
    return ranking.vector_ranks(
        items=items,
        query_vector=query_vector,
        deadline=deadline,
        clock=clock,
        is_expired=_expired,
    )


def _corpus_language(passages: list[_Passage]) -> str | None:
    """The language a bounded, deterministic sample of the corpus is written in.

    Passages arrive ordered by ref, so the sample is the same for the same snapshot on every query
    and the verdict cannot drift between two identical requests. Reading a prefix of each passage
    rather than all of it keeps this a rounding error against the BM25 scan that follows.
    """
    return language.dominant(
        passage.search_text[:_LANGUAGE_SAMPLE_CHARS]
        for passage in passages[:_LANGUAGE_SAMPLE_PASSAGES]
    )


def _detect_corpus_language(
    database: sqlite3.Connection, passages: list[_Passage] | None = None
) -> str | None:
    try:
        return SnapshotRepository(database).detect_corpus_language(passages)
    except RepositoryError as error:
        raise RetrievalError(error.code) from error


_fuse = ranking.fuse_ranks


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
    # The deadline is checked on the way OUT as well as on the way in. It cannot bound the call --
    # `rerank` is one opaque invocation over the whole head, and a caller-supplied callable cannot
    # be interrupted from here -- but the alternative was worse than not bounding it: the check at
    # the top of this function passes, forty cross-encoder passes then take as long as they take,
    # and `search` returned late reporting nothing at all. A budget that is silently exceeded reads
    # exactly like a budget that was met. The work is kept rather than discarded, because it is
    # finished and correct; only its lateness is disclosed.
    if clock() >= deadline:
        degradation.append("max_elapsed_ms_exceeded")
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


# Degradation entries that describe a rule this engine APPLIED, not something it failed to do.
#
# `reranked:N_documents` records that an opt-in stage ran; `lexical_leg_discounted:...` records a
# ranking weight chosen deliberately because the query's language does not match the corpus.
# Neither is a shortfall, and calling a response `partial` for either would make the field mean
# "something is disclosed here" instead of "you got less than this engine can give" -- which is
# the same erosion that made `complete` worthless in the first place.
_DISCLOSURE_PREFIXES = ("reranked:", "lexical_leg_discounted:")


def is_shortfall(note: str) -> bool:
    """Whether a degradation entry means the caller got LESS than a healthy request would return.

    Deliberately fail-closed: anything not named above counts as a shortfall. A future degradation
    added without revisiting this classifies as `partial`, which over-reports; the allowlist form
    would classify it as `complete`, which is the failure this function exists to end. Over-
    reporting is visible and gets fixed; under-reporting is what shipped for two releases.

    Note that `*_leg_no_matches` IS a shortfall. A leg that ran and matched nothing is a real
    answer about the corpus, but the response was still assembled from half the engine -- and this
    is exactly how embedding-dimension drift used to be reported as a complete result.
    """
    return not note.startswith(_DISCLOSURE_PREFIXES)


def _leg_state(ranks: dict[str, int] | None, leg: str, degradation: list[str]) -> None:
    # `None` means the leg could not run; an empty mapping means it ran and matched nothing.
    # Both must be reported: embedding-dimension drift produces the second and used to be silent.
    if ranks is None:
        degradation.append(f"{leg}_leg_unavailable")
    elif not ranks:
        degradation.append(f"{leg}_leg_no_matches")


class SearchService:
    """Application use case for hybrid lexical and vector search across a snapshot repository."""

    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        embed_query: EmbedQuery | None = None,
        rerank: Rerank | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repository
        self._embed_query = embed_query
        self._rerank = rerank
        self._clock = clock

    @property
    def repository(self) -> SnapshotRepository:
        return self._repo

    def search(
        self,
        query: str,
        budgets: Budgets = Budgets(),
        *,
        offset: int = 0,
    ) -> RetrievalOutcome:
        """Search across the repository. Never writes, never raises on a budget ceiling."""
        if not isinstance(query, str) or not query.strip():
            raise RetrievalError("empty_query")
        if len(query) > _MAX_QUERY_CHARS:
            raise RetrievalError("query_too_long")
        if offset < 0:
            raise RetrievalError("invalid_offset")

        deadline = self._clock() + budgets.max_elapsed_ms / 1000
        degradation: list[str] = []

        if not self._repo.has_lexical_index() or self._rerank is not None:
            lexical_ranks, vector_ranks, corpus_language, candidates_scanned, passages, by_ref = (
                self._search_full_scan(query, deadline, degradation)
            )
        else:
            lexical_ranks, vector_ranks, corpus_language, candidates_scanned, passages, by_ref = (
                self._search_fast_path(query, deadline, degradation)
            )

        lexical_weight = self._compute_lexical_weight(query, corpus_language, degradation)
        ordered = _fuse(lexical_ranks, vector_ranks, lexical_weight)

        if self._rerank is not None:
            ordered = _rerank_fused(
                ordered, passages, by_ref, query, self._rerank, deadline, self._clock, degradation
            )

        return self._paginate_and_hydrate(
            ordered, by_ref, budgets, offset, candidates_scanned, degradation
        )

    def _search_full_scan(
        self,
        query: str,
        deadline: float,
        degradation: list[str],
    ) -> tuple[
        dict[str, int] | None,
        dict[str, int] | None,
        str | None,
        int,
        list[_Passage],
        dict[str, _Passage],
    ]:
        try:
            passages, scan_stopped = self._repo.scan_passages(deadline, self._clock, _expired)
        except RepositoryError as error:
            raise RetrievalError(error.code) from error
        except sqlite3.DatabaseError as error:
            raise RetrievalError("snapshot_unreadable") from error

        candidates_scanned = len(passages)
        by_ref = {passage.ref: passage for passage in passages}
        corpus_language = _corpus_language(passages)

        if self._repo.has_lexical_index():
            lexical_ranks, lexical_stopped = _bm25_indexed_ranks(
                self._repo.database, _tokenize(query), deadline, self._clock
            )
        else:
            lexical_ranks, lexical_stopped = _bm25_ranks(
                passages, _tokenize(query), deadline, self._clock
            )
        _leg_state(lexical_ranks, "lexical", degradation)

        vector_ranks, vector_stopped = self._compute_vector_ranks(passages, query, deadline, degradation)

        if scan_stopped or lexical_stopped or vector_stopped:
            degradation.append("max_elapsed_ms_exceeded")

        return lexical_ranks, vector_ranks, corpus_language, candidates_scanned, passages, by_ref

    def _search_fast_path(
        self,
        query: str,
        deadline: float,
        degradation: list[str],
    ) -> tuple[
        dict[str, int] | None,
        dict[str, int] | None,
        str | None,
        int,
        list[_Passage],
        dict[str, _Passage],
    ]:
        try:
            vectors, scan_stopped = self._repo.scan_vectors(deadline, self._clock, _expired)
        except RepositoryError as error:
            raise RetrievalError(error.code) from error
        except sqlite3.DatabaseError as error:
            raise RetrievalError("snapshot_unreadable") from error

        candidates_scanned = len(vectors)
        lexical_ranks, lexical_stopped = _bm25_indexed_ranks(
            self._repo.database, _tokenize(query), deadline, self._clock
        )
        _leg_state(lexical_ranks, "lexical", degradation)

        vector_ranks, vector_stopped = self._compute_vector_ranks(vectors, query, deadline, degradation)

        if scan_stopped or lexical_stopped or vector_stopped:
            degradation.append("max_elapsed_ms_exceeded")

        corpus_language = self._repo.detect_corpus_language()
        return lexical_ranks, vector_ranks, corpus_language, candidates_scanned, [], {}

    def _compute_vector_ranks(
        self,
        candidates: Sequence[_Passage] | Sequence[tuple[str, bytes]],
        query: str,
        deadline: float,
        degradation: list[str],
    ) -> tuple[dict[str, int] | None, bool]:
        if self._embed_query is None:
            degradation.append("vector_leg_unavailable")
            return None, False

        try:
            query_vector = self._embed_query(query)
        except Exception as error:  # noqa: BLE001 -- embed_query is a caller-supplied untrusted callable
            query_vector = None
            degradation.append(f"vector_leg_failed:{type(error).__name__}")

        if query_vector is None:
            return None, False

        vector_ranks, vector_stopped = _vector_ranks(candidates, query_vector, deadline, self._clock)
        _leg_state(vector_ranks, "vector", degradation)
        return vector_ranks, vector_stopped

    def _compute_lexical_weight(
        self,
        query: str,
        corpus_language: str | None,
        degradation: list[str],
    ) -> float:
        lexical_weight = 1.0
        query_language = language.detect(query)
        if (
            query_language is not None
            and corpus_language is not None
            and query_language != corpus_language
        ):
            lexical_weight = _CROSS_LINGUAL_LEXICAL_WEIGHT
            degradation.append(f"lexical_leg_discounted:{query_language}_query_{corpus_language}_corpus")
        return lexical_weight

    def _paginate_and_hydrate(
        self,
        ordered: list[tuple[str, int | None, int | None]],
        by_ref: dict[str, _Passage],
        budgets: Budgets,
        offset: int,
        candidates_scanned: int,
        degradation: list[str],
    ) -> RetrievalOutcome:
        matches: list[RetrievalMatch] = []
        truncated = False
        extracted = 0
        ordered_slice = ordered[offset:] if offset < len(ordered) else []
        if not by_ref and ordered_slice:
            needed_refs = [ref for ref, _, _ in ordered_slice[: budgets.max_candidates]]
            try:
                by_ref = self._repo.hydrate_passages(needed_refs)
            except RepositoryError as error:
                raise RetrievalError(error.code) from error
            except sqlite3.DatabaseError as error:
                raise RetrievalError("snapshot_unreadable") from error

        for rank, (ref, lexical_rank, vector_rank) in enumerate(ordered_slice, start=offset + 1):
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
                    ref=passage.ref,
                    document_ref=passage.document_ref,
                    relative_path=passage.relative_path,
                    heading_path=passage.heading_path,
                    start_line=passage.start_line,
                    end_line=passage.end_line,
                    snippet=snippet,
                    source_hash=passage.source_hash,
                    rank=rank,
                    lexical_rank=lexical_rank,
                    vector_rank=vector_rank,
                )
            )

        warnings = ["no_eligible_results"] if not matches and offset == 0 else []
        return RetrievalOutcome(
            matches=tuple(matches),
            degradation=tuple(dict.fromkeys(degradation)),
            warnings=tuple(warnings),
            candidates_scanned=candidates_scanned,
            truncated=truncated,
        )


def search(
    snapshot: ActiveSnapshot | SnapshotRepository,
    query: str,
    budgets: Budgets = Budgets(),
    *,
    offset: int = 0,
    embed_query: EmbedQuery | None = None,
    rerank: Rerank | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RetrievalOutcome:
    """Search the given open snapshot or repository. Never writes, never raises on a budget ceiling."""
    repo = snapshot if isinstance(snapshot, SnapshotRepository) else SnapshotRepository(snapshot.database)
    service = SearchService(repo, embed_query=embed_query, rerank=rerank, clock=clock)
    return service.search(query, budgets, offset=offset)


def build_local_evidence_record(
    *,
    ref: str,
    document_ref: str,
    start_line: int,
    end_line: int,
    source_hash: str,
    authority: Literal["primary", "official", "standard", "contextual", "unknown"],
    authority_rationale: AuthorityRationale,
    extraction_method: Literal[
        "raw_lines", "markdown_section", "html_text", "pdf_text", "api_json", "unknown"
    ] = "markdown_section",
    provenance_chain: Sequence[str] = (),
    uncertainty: Sequence[str] = (),
    freshness: Literal["current", "stale", "expired", "unknown"] = "unknown",
    license: Literal["permitted", "restricted", "prohibited", "unknown"] = "unknown",
    reuse: Literal["permitted", "restricted", "prohibited", "unknown"] = "unknown",
    conflict: Literal["none", "declared", "unknown"] = "unknown",
) -> EvidenceRecord:
    """The ONE place every local-corpus `EvidenceRecord` is built (T2, investigate-boundary-v2:
    opaque evidence locators). `locator` is the passage's own `document_ref`
    (`doc:v1:<hash>`, see `corpus.py::parse_document`), never the author-chosen relative path;
    `citation_locator` is `f"{document_ref}#L{start}-{end}"`; `publisher` is the fixed
    `LOCAL_EVIDENCE_PUBLISHER` literal. `authority_rationale` is one of the closed
    `AuthorityRationale` codes the schema enforces -- never free corpus text. Every producer of a
    local `EvidenceRecord` (`to_evidence_records` below, `service.py::_apply_lineage`,
    `_resolve_code_target_causality`, `_evaluate_counterfactual`) routes through this builder, so
    the boundary is enforced once rather than asserted per call site."""
    return EvidenceRecord(
        ref=ref, kind="local", publisher=LOCAL_EVIDENCE_PUBLISHER,
        locator=document_ref, citation_locator=f"{document_ref}#L{start_line}-{end_line}",
        digest=f"sha256:{source_hash}", extraction_method=extraction_method,
        provenance_chain=list(provenance_chain),
        authority=authority, authority_rationale=authority_rationale,
        freshness=freshness, license=license, reuse=reuse, conflict=conflict,
        uncertainty=list(uncertainty),
    )


def to_evidence_records(outcome: RetrievalOutcome) -> list[EvidenceRecord]:
    """Show the output is expressible in the closed evidence model; every assessment field is
    this slice's conservative "unknown" -- never inferred from rank."""
    return [
        build_local_evidence_record(
            ref=match.ref, document_ref=match.document_ref,
            start_line=match.start_line, end_line=match.end_line, source_hash=match.source_hash,
            authority="unknown", authority_rationale="not_assessed_by_retrieval",
        )
        for match in outcome.matches
    ]
