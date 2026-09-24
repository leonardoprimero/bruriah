#!/usr/bin/env python3
"""For each question, the rank of (a) the ground-truth commit document, (b) that same rank with
every GitHub-derived document removed from the ranking first, and (c) the question's OWN GitHub
issue document, in the untruncated fused DOCUMENT order -- the same order `report_reach.py` reads.

    python evals/retrieval/report_issue_document_rank.py --data-dir /tmp/data-leakcanary-github \
        --questions evals/project-memory/leakcanary-issues.jsonl --out /tmp/lc-issue-doc-ranks.jsonl

WHY A THIRD RANK, NOT JUST `report_paired.py`'s BASELINE-VS-CANDIDATE. `report_paired.py` answers
whether the correct COMMIT document moved once GitHub documents entered the corpus -- and on this
question set it moves down, because these evaluation questions were themselves built from issue
titles (see `evals/project-memory/README.md`, "Issue ingestion, measured 2026-09-21"): the
question's own issue document is a near-verbatim vocabulary match and outranks the commit that
closed it. That is expected, not a regression, and `truth_rank_commit_only` -- the same commit
document's rank with every GitHub document filtered out of the SAME ranking, not a second index
build -- is what actually answers "does ingestion degrade commit retrieval": it does not have to
compete with the issue documents it was never meant to beat.

WHY NOT REUSE `report_reach.py`'s FUNCTION DIRECTLY. Its `document_rank_of_answer` returns the rank
of one fixed name and stops there; this script needs three different ranks read off the SAME
underlying order per question (commit truth, commit-only truth, own issue document), which is
cheaper to compute once per query than to call three times, so `ranking()` here returns the full
document order and the pure functions below index into it.

Like `report_reach.py`, this reaches into `bruriah.retrieval`'s private `_bm25_ranks`/
`_vector_ranks`/`_fuse` to see the ranking BEFORE `search`'s public candidate ceiling truncates it
-- see that script's docstring for why that reach is deliberate. No reranker, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import strip_build_sha  # noqa: E402

from bruriah import language  # noqa: E402
from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import (  # noqa: E402
    _CROSS_LINGUAL_LEXICAL_WEIGHT,
    _bm25_ranks,
    _corpus_language,
    _fuse,
    _scan_passages,
    _tokenize,
    _vector_ranks,
)

_NO_DEADLINE = float("inf")

# The exact convention `github_corpus.build_documents` writes and `report_counterfactuals.py`
# already reads a document's provenance from: `f"{date10}-issue-{number}-{slug}.md"`, `date10`
# either an ISO date's first 10 characters or the fixed string `"unknown-date"`.
_ISSUE_DOCUMENT_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|unknown-date)-issue-(\d+)-.+\.md$")


def _never_expires() -> float:
    return 0.0


def issue_number_of(document_name: str) -> str | None:
    """The issue number a GitHub-derived document's filename names, or `None` for a commit
    document. Pure string matching -- no index, no I/O -- so it is tested directly."""
    match = _ISSUE_DOCUMENT_RE.match(document_name)
    return match.group(1) if match else None


def document_rank(order: list[str], name: str) -> int | None:
    """1-indexed position of `name` in `order`, or `None` if absent."""
    return order.index(name) + 1 if name in order else None


def issue_document_rank(order: list[str], issue: str) -> int | None:
    """1-indexed position of the FIRST document in `order` whose filename names `issue`."""
    for position, name in enumerate(order, 1):
        if issue_number_of(name) == issue:
            return position
    return None


def commit_only_order(order: list[str]) -> list[str]:
    """`order` with every GitHub-derived document removed, ranks otherwise unchanged -- answers
    where the commit truth would rank if it only had to compete with commit documents, the
    question `report_paired.py`'s baseline-vs-candidate comparison cannot isolate on its own."""
    return [name for name in order if issue_number_of(name) is None]


@dataclass(frozen=True)
class IssueDocumentRankRow:
    """One question's three ranks, plus whether the top hit is (a) any GitHub document and (b) the
    question's own issue document specifically."""

    id: str
    issue: str
    truth_rank: int | None
    truth_rank_commit_only: int | None
    issue_doc_rank: int | None
    top1_is_issue_doc: bool
    top1_is_own_issue: bool


def score_question(question_id: str, order: list[str], truth: str, issue: str) -> IssueDocumentRankRow:
    """Pure scoring: everything `main` needs to know about one question's ranking, given the
    document order already read for it and the question's own truth document name and issue
    number. No I/O, so this is the function the tests exercise directly."""
    top1 = order[0] if order else None
    return IssueDocumentRankRow(
        id=question_id,
        issue=issue,
        truth_rank=document_rank(order, truth),
        truth_rank_commit_only=document_rank(commit_only_order(order), truth),
        issue_doc_rank=issue_document_rank(order, issue),
        top1_is_issue_doc=bool(top1 and issue_number_of(top1) is not None),
        top1_is_own_issue=bool(top1 and issue_number_of(top1) == issue),
    )


def ranking(passages, by_ref, corpus_language, deps, query: str) -> list[str]:
    """The untruncated fused DOCUMENT order for `query`: the same legs `search` walks
    (`_bm25_ranks`, `_vector_ranks`, `_fuse`), restated here rather than imported from
    `report_reach.py`, whose `document_rank_of_answer` scores one fixed name and returns instead
    of exposing the order itself."""
    lexical, _stopped = _bm25_ranks(passages, _tokenize(query), _NO_DEADLINE, _never_expires)
    vector, _stopped = _vector_ranks(passages, deps.embed_query(query), _NO_DEADLINE, _never_expires)
    weight = 1.0
    query_language = language.detect(query)
    if query_language is not None and corpus_language is not None and query_language != corpus_language:
        weight = _CROSS_LINGUAL_LEXICAL_WEIGHT
    seen: list[str] = []
    for ref, _lexical_rank, _vector_rank in _fuse(lexical, vector, weight):
        name = strip_build_sha(by_ref[ref].relative_path.rsplit("/", 1)[-1])
        if name not in seen:
            seen.append(name)
    return seen


def render(rows: list[IssueDocumentRankRow]) -> str:
    n = len(rows)
    if n == 0:
        return "n=0"

    def r_at(key: str, k: int) -> float:
        return sum(1 for row in rows if getattr(row, key) is not None and getattr(row, key) <= k) / n

    rank1 = sum(1 for row in rows if row.issue_doc_rank == 1)
    missing = sum(1 for row in rows if row.issue_doc_rank is None)
    above = sum(
        1
        for row in rows
        if row.truth_rank is not None and row.issue_doc_rank is not None and row.issue_doc_rank < row.truth_rank
    )
    return "\n".join(
        [
            f"n={n}",
            f"commit truth: recall@3={r_at('truth_rank', 3):.3f} recall@10={r_at('truth_rank', 10):.3f}",
            f"commit truth among commit docs only: recall@3={r_at('truth_rank_commit_only', 3):.3f} "
            f"recall@10={r_at('truth_rank_commit_only', 10):.3f}",
            f"own issue doc: recall@3={r_at('issue_doc_rank', 3):.3f} "
            f"recall@10={r_at('issue_doc_rank', 10):.3f} rank1={rank1} missing_doc={missing}",
            f"own issue doc ranked above the commit truth: {above}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="per-question ranks, as JSONL")
    args = parser.parse_args(argv)

    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
    deps = build_serve_deps(resolve_paths(cli_data_dir=args.data_dir, env={}))
    rows: list[IssueDocumentRankRow] = []
    try:
        passages, _stopped = _scan_passages(deps.snapshot.database, _NO_DEADLINE, _never_expires)
        by_ref = {passage.ref: passage for passage in passages}
        corpus_language = _corpus_language(passages)
        for question in questions:
            order = ranking(passages, by_ref, corpus_language, deps, question["query"])
            truth = strip_build_sha(question["ground_truth"]["must_include"][0])
            issue = str((question.get("provenance") or {}).get("issue"))
            rows.append(score_question(question["id"], order, truth, issue))
    finally:
        deps.snapshot.database.close()

    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(asdict(row), sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
