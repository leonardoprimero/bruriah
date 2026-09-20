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
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import language, ranking
from .contracts import Budgets, EvidenceRecord
from .index import ActiveSnapshot

EmbedQuery = Callable[[str], bytes]
# A reranker scores whole documents against the query and returns one number each, higher first.
# Supplied by the caller exactly as `embed_query` is, for the same reason: the model, its download
# and its licence are the operator's choice, and retrieval must stay importable without either.
Rerank = Callable[[str, list[str]], list[float]]
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_BM25_K1 = ranking.BM25_K1
_BM25_B = ranking.BM25_B
_RRF_K = ranking.RRF_K

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
_CROSS_LINGUAL_LEXICAL_WEIGHT = ranking.CROSS_LINGUAL_LEXICAL_WEIGHT

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
#
# This note used to say reranking LOSES on `emilk/egui` (0.494 against 0.530) and loses deeper.
# That was measured before d767aab, and d767aab fixed the exact mechanism it observed: the
# cross-encoder reading a 14,071-character egui commit's pull-request-template header instead of
# the passage that matched. Re-measured on the same jina configuration afterwards, both depths
# now BEAT the baseline -- 0.518 no-reranker, 0.542 at depth 40, 0.566 at depth 20. The loss was
# a property of the bug, not of the corpus.
#
# What remains is the ordinary version: depth 20 wins on egui, depth 40 wins on leakcanary
# (0.431 against 0.412), so the best depth depends on the corpus while no measured corpus is hurt
# by the stage. That is still the whole reason 40 is a default rather than a tuned value, and the
# reason the stage is opt-in: which depth suits a corpus is not predictable from anything measured
# here, so measure it on yours with `evals/retrieval/run_ablation.py --depth`.
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
    # `text` is the section's own bytes and `search_text` is that section under its heading
    # ancestry. Only the second is scored: see `corpus._search_text`. Everything a human or a
    # caller is shown -- snippets, rerank input, evidence -- comes from `text`.
    search_text: str
    source_hash: str
    vector: bytes


def _tokenize(text: str) -> tuple[str, ...]:
    return language.tokenize(text)


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
        "text, search_text, source_hash, vector FROM passages ORDER BY ref"
    )
    passages: list[_Passage] = []
    stopped = False
    for position, row in enumerate(rows):
        if _expired(position, deadline, clock):
            stopped = True
            break
        (
            ref, document_ref, relative_path, heading_json, start_line, end_line, text,
            search_text, source_hash, vector,
        ) = row
        passages.append(
            _Passage(
                ref, document_ref, relative_path, _heading_path(heading_json),
                start_line, end_line, text, search_text, source_hash, vector,
            )
        )
    return passages, stopped


def _scan_vectors(
    database: sqlite3.Connection, deadline: float, clock: Callable[[], float]
) -> tuple[list[tuple[str, bytes]], bool]:
    rows = database.execute("SELECT ref, vector FROM passages ORDER BY ref")
    vectors: list[tuple[str, bytes]] = []
    stopped = False
    for position, (ref, vector) in enumerate(rows):
        if _expired(position, deadline, clock):
            stopped = True
            break
        vectors.append((ref, vector))
    return vectors, stopped


def _hydrate_passages(
    database: sqlite3.Connection, refs: Sequence[str]
) -> dict[str, _Passage]:
    if not refs:
        return {}
    placeholders = ", ".join("?" for _ in refs)
    rows = database.execute(
        f"SELECT ref, document_ref, relative_path, heading_path, start_line, end_line, "
        f"text, search_text, source_hash, vector FROM passages WHERE ref IN ({placeholders})",
        list(refs),
    )
    hydrated: dict[str, _Passage] = {}
    for row in rows:
        (
            ref, document_ref, relative_path, heading_json, start_line, end_line, text,
            search_text, source_hash, vector,
        ) = row
        hydrated[ref] = _Passage(
            ref, document_ref, relative_path, _heading_path(heading_json),
            start_line, end_line, text, search_text, source_hash, vector,
        )
    return hydrated


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
    try:
        row = database.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('corpus_stats', 'term_df', 'term_postings')"
        ).fetchone()
        return bool(row and row[0] == 3)
    except sqlite3.Error:
        return False


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

    stats_rows = database.execute(
        "SELECT key, num_value FROM corpus_stats"
    ).fetchall()
    stats = {key: num_val for key, num_val in stats_rows}
    total_documents = int(stats.get("total_documents", 0.0))
    average_length = float(stats.get("average_length", 0.0))

    if total_documents == 0:
        return None, False
    if average_length == 0.0:
        return {}, False

    unique_terms = sorted(set(query_tokens))
    placeholders = ", ".join("?" for _ in unique_terms)
    df_rows = database.execute(
        f"SELECT term, df FROM term_df WHERE term IN ({placeholders})",
        unique_terms,
    ).fetchall()
    dfs = dict(df_rows)
    if not dfs:
        return {}, False

    query_terms = sorted(dfs.keys())
    postings_placeholders = ", ".join("?" for _ in query_terms)
    cursor = database.execute(
        f"SELECT term, ref, freq, doc_length FROM term_postings WHERE term IN ({postings_placeholders}) ORDER BY term, ref",
        query_terms,
    )

    return ranking.bm25_scores_from_postings(
        total_documents=total_documents,
        average_length=average_length,
        dfs=dfs,
        postings=cursor,
        deadline=deadline,
        clock=clock,
        is_expired=_expired,
        k1=_BM25_K1,
        b=_BM25_B,
    )


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
    if _has_lexical_index(database):
        row = database.execute(
            "SELECT str_value FROM corpus_stats WHERE key = 'corpus_language'"
        ).fetchone()
        if row is not None and row[0] is not None:
            return row[0]
    if passages is not None:
        return _corpus_language(passages)
    sample_rows = database.execute(
        f"SELECT search_text FROM passages ORDER BY ref LIMIT {_LANGUAGE_SAMPLE_PASSAGES}"
    ).fetchall()
    return language.dominant(text[:_LANGUAGE_SAMPLE_CHARS] for text, in sample_rows)


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


def search(
    snapshot: ActiveSnapshot,
    query: str,
    budgets: Budgets = Budgets(),
    *,
    offset: int = 0,
    embed_query: EmbedQuery | None = None,
    rerank: Rerank | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RetrievalOutcome:
    """Search the given open, read-only snapshot. Never writes, never raises on a budget ceiling."""
    if not isinstance(query, str) or not query.strip():
        raise RetrievalError("empty_query")
    if len(query) > _MAX_QUERY_CHARS:
        raise RetrievalError("query_too_long")
    if offset < 0:
        raise RetrievalError("invalid_offset")

    deadline = clock() + budgets.max_elapsed_ms / 1000
    degradation: list[str] = []
    has_index = _has_lexical_index(snapshot.database)

    by_ref: dict[str, _Passage] = {}
    if not has_index or rerank is not None:
        try:
            passages, scan_stopped = _scan_passages(snapshot.database, deadline, clock)
        except sqlite3.DatabaseError as error:
            raise RetrievalError("snapshot_unreadable") from error
        candidates_scanned = len(passages)
        by_ref = {passage.ref: passage for passage in passages}
        corpus_language = _corpus_language(passages)
        if has_index:
            lexical_ranks, lexical_stopped = _bm25_indexed_ranks(
                snapshot.database, _tokenize(query), deadline, clock
            )
        else:
            lexical_ranks, lexical_stopped = _bm25_ranks(
                passages, _tokenize(query), deadline, clock
            )
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
    else:
        # Fast path: precomputed lexical index + lazy passage hydration
        try:
            vectors, scan_stopped = _scan_vectors(snapshot.database, deadline, clock)
        except sqlite3.DatabaseError as error:
            raise RetrievalError("snapshot_unreadable") from error
        candidates_scanned = len(vectors)

        lexical_ranks, lexical_stopped = _bm25_indexed_ranks(
            snapshot.database, _tokenize(query), deadline, clock
        )
        _leg_state(lexical_ranks, "lexical", degradation)

        vector_ranks = None
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
                vector_ranks, vector_stopped = _vector_ranks(vectors, query_vector, deadline, clock)
                _leg_state(vector_ranks, "vector", degradation)

        if scan_stopped or lexical_stopped or vector_stopped:
            degradation.append("max_elapsed_ms_exceeded")

        corpus_language = _detect_corpus_language(snapshot.database)

    # Discount the lexical leg when the question is not in the language the corpus is written in.
    # Both legs still run and both ranks are still reported: this changes the weight of evidence,
    # never which evidence exists. Disclosed in `degradation` rather than applied silently, because
    # a caller comparing two result sets is entitled to know the ranking rule was not the same.
    lexical_weight = 1.0
    query_language = language.detect(query)
    if query_language is not None and corpus_language is not None \
            and query_language != corpus_language:
        lexical_weight = _CROSS_LINGUAL_LEXICAL_WEIGHT
        degradation.append(f"lexical_leg_discounted:{query_language}_query_{corpus_language}_corpus")

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
    ordered_slice = ordered[offset:] if offset < len(ordered) else []
    if not by_ref and ordered_slice:
        needed_refs = [ref for ref, _, _ in ordered_slice[: budgets.max_candidates]]
        try:
            by_ref = _hydrate_passages(snapshot.database, needed_refs)
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
                ref=passage.ref, document_ref=passage.document_ref, relative_path=passage.relative_path,
                heading_path=passage.heading_path, start_line=passage.start_line, end_line=passage.end_line,
                snippet=snippet, source_hash=passage.source_hash,
                rank=rank, lexical_rank=lexical_rank, vector_rank=vector_rank,
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
