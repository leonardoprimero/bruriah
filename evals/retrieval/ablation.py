"""Pure scoring for the reranker ablation: where the correct document sat before a cross-encoder
reordered the pool, and where it sat after.

`evals/project-memory/README.md` publishes one number per corpus per condition, and two corpora
cannot support a claim about WHEN reranking helps -- any two points are consistent with any
explanation. This module scores the same runs one question at a time, so the 236-question set can
answer a question the two averages cannot.

THE TRAP THIS MODULE EXISTS TO AVOID. The obvious analysis is to bucket questions by the rank the
shipped ranking gave the correct document and ask how far reranking moved it. That analysis is
rigged. A document already at rank 1 can only move down; one at rank 40 can only move up. A
reranker that assigned scores by coin flip would produce exactly the shape a reader would read as
"reranking hurts where the base ranking was strong" -- regression to the mean, wearing the costume
of a finding.

So every movement figure here is reported against a null: the same head, permuted uniformly at
random, scored the same way. `expected_rank_under_shuffle` gives that null in closed form rather
than by sampling, because a permutation test that reports a different number on each run cannot be
committed as a reproducible figure. Only movement that beats the null is evidence about the
reranker rather than about the arithmetic of bounded ranks.

No I/O, no wall-clock, no randomness: `run_ablation.py` does the searching and calls in here.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

# `bruriah corpus` writes `{date}-{sha8}-{slug}.md`; recorded ground truth carries no sha, because a
# sha does not survive a history rewrite and this eval is meant to outlive one. Documented at
# `evals/project-memory/README.md`, which also names the failure of forgetting it: the scorer
# reports 0% and reads as a retrieval collapse rather than a name mismatch.
_BUILD_SHA = re.compile(r"^(\d{4}-\d{2}-\d{2})-[0-9a-f]{8}-")

# Buckets over the rank the SHIPPED ranking gave the correct document. The first three are the
# region a user actually reads; `11-40` is still inside the reranked head and can move either way;
# `41+` is past `_RERANK_DEPTH`, so those questions are carried to be counted and can never move.
_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("1", 1, 1),
    ("2-3", 2, 3),
    ("4-10", 4, 10),
    ("11-40", 11, 40),
    ("41+", 41, 1 << 30),
)


def strip_build_sha(document_name: str) -> str:
    """`2015-05-10-c2d938fb-customizable-excludedref.md` -> `2015-05-10-customizable-excludedref.md`.

    Anchored on the date so a slug that happens to start with eight hex characters is left alone.
    A name that does not carry a sha comes back unchanged, which is what makes this safe to apply
    to both sides of a comparison.
    """
    return _BUILD_SHA.sub(r"\1-", document_name)


def document_ranking(relative_paths: Sequence[str]) -> list[str]:
    """Collapse a passage-ordered result list to a document-ordered one, best position wins.

    `search` ranks passages and a document may own several, so the rank a reader experiences is the
    position of a document's FIRST passage. Keeping the first occurrence is only correct because
    `outcome.matches` is already fused-sorted -- the same precondition `adapters.dedup_to_notes`
    relies on, restated here rather than imported because this module stays free of the engine.
    """
    seen: set[str] = set()
    ordering: list[str] = []
    for path in relative_paths:
        name = strip_build_sha(path.rsplit("/", 1)[-1])
        if name not in seen:
            seen.add(name)
            ordering.append(name)
    return ordering


def rank_of(ranking: Sequence[str], ground_truth: str) -> int | None:
    """1-indexed position of `ground_truth` in `ranking`, or `None` when it never appears.

    `None` is not rank infinity and callers must not average it in as one: a document outside the
    pool was never offered to the reranker, so it carries no information about reordering. It is
    counted separately, in `AblationSummary.outside_pool`.
    """
    target = strip_build_sha(ground_truth)
    for position, name in enumerate(ranking, start=1):
        if name == target:
            return position
    return None


@dataclass(frozen=True)
class QuestionOutcome:
    """One question scored under both conditions, as written to the per-question artifact.

    `base_rank`/`reranked_rank` are `None` when the correct document was not in that condition's
    returned set. THE TWO DISAGREE, and an earlier version of this docstring said they could not:
    it reasoned that `retrieval._rerank_fused` reorders the head and carries the tail, so
    membership is preserved. That is true of `_rerank_fused` and false of `search`, which truncates
    AFTERWARDS on `max_candidates` and `max_extracted_chars`.

    The mechanism, measured rather than reasoned: the fused order ranks PASSAGES, so its head is
    drawn from many documents at one passage each, while `_rerank_fused` groups every passage of a
    document together. Under the same character budget, one leakcanary query returned 48 documents
    across 48 passages unranked and 25 documents across 49 passages reranked. Both `pool_documents`
    figures are therefore recorded: a document count that halves is the finding, and keeping only
    one of them is what hid it.
    """

    id: str
    corpus: str
    base_rank: int | None
    reranked_rank: int | None
    pool_documents: int
    rerank_depth: int
    reranked_pool_documents: int | None = None

    @property
    def moved(self) -> int | None:
        """Positions gained; positive means the correct document moved UP. `None` if unscoreable."""
        if self.base_rank is None or self.reranked_rank is None:
            return None
        return self.base_rank - self.reranked_rank


def expected_rank_under_shuffle(base_rank: int, head: int) -> float:
    """Where a uniformly random permutation of the reranked head would put this document.

    Closed form, not sampled: a permutation test drawing its own randomness would report a
    different figure on every run, and a number that moves cannot be committed next to a claim.

    A document inside the head lands uniformly on `1..head`, so its expected rank is `(head+1)/2`
    regardless of where it started. A document PAST the head is never handed to the reranker and
    cannot move, so its expected rank is exactly where it already is. That asymmetry is the whole
    reason the null is needed: it is also the shape a real reranker produces, and only the
    DIFFERENCE between them says anything about the model.
    """
    if base_rank > head:
        return float(base_rank)
    return (head + 1) / 2


def bucket_of(base_rank: int) -> str:
    for label, low, high in _BUCKETS:
        if low <= base_rank <= high:
            return label
    raise ValueError(f"unbucketable rank: {base_rank}")


@dataclass(frozen=True)
class BucketSummary:
    """One base-rank bucket, observed against the shuffle null.

    `mean_reranked_rank` beating `mean_expected_rank` (lower is better) is the only statement in
    this module that is evidence about the cross-encoder. `mean_moved` on its own is not: it is
    bounded by where the document started, which is what the bucket selected on.
    """

    bucket: str
    n: int
    helped: int
    hurt: int
    unchanged: int
    mean_base_rank: float | None
    mean_reranked_rank: float | None
    mean_expected_rank: float | None
    recall_at_3_before: float | None
    recall_at_3_after: float | None

    @property
    def beats_null(self) -> float | None:
        """Positions better than a coin flip would have managed. Positive means real signal."""
        if self.mean_reranked_rank is None or self.mean_expected_rank is None:
            return None
        return self.mean_expected_rank - self.mean_reranked_rank


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_bucket(label: str, outcomes: Sequence[QuestionOutcome]) -> BucketSummary:
    scored = [outcome for outcome in outcomes if outcome.moved is not None]
    moves = [outcome.moved for outcome in scored]
    return BucketSummary(
        bucket=label,
        n=len(scored),
        helped=sum(1 for move in moves if move > 0),
        hurt=sum(1 for move in moves if move < 0),
        unchanged=sum(1 for move in moves if move == 0),
        mean_base_rank=_mean([float(outcome.base_rank) for outcome in scored]),
        mean_reranked_rank=_mean([float(outcome.reranked_rank) for outcome in scored]),
        mean_expected_rank=_mean(
            [expected_rank_under_shuffle(outcome.base_rank, outcome.rerank_depth) for outcome in scored]
        ),
        recall_at_3_before=_mean([1.0 if outcome.base_rank <= 3 else 0.0 for outcome in scored]),
        recall_at_3_after=_mean([1.0 if outcome.reranked_rank <= 3 else 0.0 for outcome in scored]),
    )


@dataclass(frozen=True)
class AblationSummary:
    """`evicted`/`rescued` are the asymmetry the docstring on `QuestionOutcome` used to deny.

    `evicted` counts questions whose correct document the shipped ranking returned and the reranked
    one did not. It is reported next to the recall figures rather than derived on demand, because a
    stage that raises recall@3 while dropping documents out of the answer entirely is doing two
    things and an average shows one of them.
    """

    corpus: str
    questions: int
    outside_pool: int
    evicted: int
    rescued: int
    mean_pool_documents: float | None
    mean_reranked_pool_documents: float | None
    recall_at_3_before: float | None
    recall_at_3_after: float | None
    buckets: tuple[BucketSummary, ...]


def summarize(corpus: str, outcomes: Iterable[QuestionOutcome]) -> AblationSummary:
    """Aggregate one corpus, overall and per base-rank bucket.

    `recall_at_3_*` at this level counts a document outside the pool as a miss, because that is what
    a user experiences; the bucket-level figures score only what was in the pool, because a bucket
    is a statement about reordering. Both are reported rather than one, since a reader comparing
    them to the published table needs the first and a reader reading the buckets needs the second.
    """
    every = list(outcomes)
    scoreable = [outcome for outcome in every if outcome.moved is not None]
    by_bucket: dict[str, list[QuestionOutcome]] = {label: [] for label, _low, _high in _BUCKETS}
    for outcome in scoreable:
        by_bucket[bucket_of(outcome.base_rank)].append(outcome)
    return AblationSummary(
        corpus=corpus,
        questions=len(every),
        outside_pool=len(every) - len(scoreable),
        evicted=sum(
            1 for outcome in every if outcome.base_rank is not None and outcome.reranked_rank is None
        ),
        rescued=sum(
            1 for outcome in every if outcome.base_rank is None and outcome.reranked_rank is not None
        ),
        mean_pool_documents=_mean([float(outcome.pool_documents) for outcome in every]),
        mean_reranked_pool_documents=_mean(
            [float(outcome.reranked_pool_documents) for outcome in every
             if outcome.reranked_pool_documents is not None]
        ),
        recall_at_3_before=_mean(
            [1.0 if outcome.base_rank is not None and outcome.base_rank <= 3 else 0.0 for outcome in every]
        ),
        recall_at_3_after=_mean(
            [1.0 if outcome.reranked_rank is not None and outcome.reranked_rank <= 3 else 0.0 for outcome in every]
        ),
        buckets=tuple(
            summarize_bucket(label, by_bucket[label]) for label, _low, _high in _BUCKETS if by_bucket[label]
        ),
    )


@dataclass(frozen=True)
class Reach:
    """How deep a caller would have to look before the correct document appeared.

    `never_ranked` is the figure that decides which problem this project has. A document the fused
    order never ranks at all is a RETRIEVAL failure and no amount of reordering reaches it; one
    ranked at 26 of 604 is an ORDERING failure wearing a budget's clothes, and the two call for
    opposite work. `search` cannot answer this -- `Budgets.max_candidates` tops out at 200 -- so it
    can only report that the answer was not in what it returned, which reads like the first and has
    so far always been the second.
    """

    total_documents: int
    questions: int
    never_ranked: int
    median_rank: int | None
    within: tuple[tuple[int, int, float], ...]


def reach(ranks: Sequence[int | None], ceilings: Sequence[int], total_documents: int) -> Reach:
    """Cumulative share of questions whose answer sits within each ceiling.

    `None` in `ranks` means never ranked and counts against every ceiling, because a caller who
    looked that deep would still not have it -- unlike `rank_of`, where `None` had to stay out of
    the averages. The difference is that this metric is about coverage and that one was about
    movement.
    """
    present = sorted(rank for rank in ranks if rank is not None)
    return Reach(
        total_documents=total_documents,
        questions=len(ranks),
        never_ranked=len(ranks) - len(present),
        median_rank=present[len(present) // 2] if present else None,
        within=tuple(
            (ceiling, sum(1 for rank in present if rank <= ceiling),
             sum(1 for rank in present if rank <= ceiling) / len(ranks) if ranks else 0.0)
            for ceiling in ceilings if ceiling <= total_documents
        ),
    )


def rescore(outcome: QuestionOutcome, **changes: object) -> QuestionOutcome:
    """Small helper so tests can vary one field without restating the record."""
    return replace(outcome, **changes)  # type: ignore[arg-type]


__all__ = [
    "AblationSummary",
    "BucketSummary",
    "QuestionOutcome",
    "bucket_of",
    "document_ranking",
    "expected_rank_under_shuffle",
    "rank_of",
    "rescore",
    "strip_build_sha",
    "summarize",
    "summarize_bucket",
]
