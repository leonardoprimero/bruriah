"""Tests for `evals/retrieval/report_issue_document_rank.py`'s pure helpers (T4, "promote the two
ablation scripts", of `odd/tasks/github-issue-pr-ingestion.md`): the filename convention that
identifies a GitHub-derived document, the commit-only ranking it derives from the full one, and the
per-question scoring built on top of both.

No index, no corpus, no network: `ranking()` and `main()` need a real snapshot database (their
`_scan_passages`/`_bm25_ranks`/`_vector_ranks`/`_fuse` calls come straight from
`bruriah.retrieval`, exactly what `report_reach.py` already reaches into) and are exercised
manually against a `--github`-built index instead, the way `evals/project-memory/README.md`'s
"Issue ingestion, measured 2026-09-21" section documents. Everything test-worthy without an index
-- the filename regex, the commit-only filter, and the score-a-single-question arithmetic -- is
pure and covered here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_EVALS_RETRIEVAL = _ROOT / "evals" / "retrieval"
if str(_EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(_EVALS_RETRIEVAL))

import report_issue_document_rank as ridr  # noqa: E402


def test_issue_number_of_matches_the_github_corpus_filename_convention() -> None:
    assert ridr.issue_number_of("2026-09-01-issue-41-some-title.md") == "41"
    assert ridr.issue_number_of("unknown-date-issue-7-no-dates-at-all.md") == "7"


def test_issue_number_of_returns_none_for_a_commit_document() -> None:
    assert ridr.issue_number_of("2024-01-01-deadbeef-unrelated-decision.md") is None
    assert ridr.issue_number_of("2024-01-01-issue-not-actually-a-number.md") is None


def test_document_rank_is_one_indexed_and_none_when_absent() -> None:
    order = ["a.md", "b.md", "c.md"]
    assert ridr.document_rank(order, "a.md") == 1
    assert ridr.document_rank(order, "c.md") == 3
    assert ridr.document_rank(order, "missing.md") is None


def test_issue_document_rank_finds_the_first_matching_issue_document() -> None:
    order = [
        "2024-01-01-deadbeef-decision.md",
        "2026-09-01-issue-41-title-one.md",
        "2026-09-02-issue-41-title-two.md",  # a second document for the same issue, ranked lower
    ]
    assert ridr.issue_document_rank(order, "41") == 2
    assert ridr.issue_document_rank(order, "99") is None


def test_commit_only_order_drops_github_documents_and_keeps_relative_order() -> None:
    order = [
        "2026-09-01-issue-41-title.md",
        "2024-01-01-deadbeef-first-commit.md",
        "2026-09-02-issue-7-other-title.md",
        "2024-02-02-cafebabe-second-commit.md",
    ]
    assert ridr.commit_only_order(order) == [
        "2024-01-01-deadbeef-first-commit.md", "2024-02-02-cafebabe-second-commit.md",
    ]


def test_score_question_ranks_commit_truth_commit_only_truth_and_own_issue_document() -> None:
    truth = "2024-01-01-deadbeef-the-fix.md"
    order = [
        "2026-09-01-issue-41-title.md",  # top-1, the question's own issue document
        truth,
        "2024-02-02-cafebabe-unrelated.md",
    ]

    row = ridr.score_question("q1", order, truth, "41")

    assert row.id == "q1"
    assert row.issue == "41"
    assert row.truth_rank == 2
    assert row.truth_rank_commit_only == 1  # only the two commit docs remain, truth is first
    assert row.issue_doc_rank == 1
    assert row.top1_is_issue_doc is True
    assert row.top1_is_own_issue is True


def test_score_question_top1_is_issue_doc_but_not_the_question_s_own_issue() -> None:
    truth = "2024-01-01-deadbeef-the-fix.md"
    order = ["2026-09-01-issue-7-someone-elses-issue.md", truth]

    row = ridr.score_question("q1", order, truth, "41")

    assert row.top1_is_issue_doc is True
    assert row.top1_is_own_issue is False
    assert row.issue_doc_rank is None  # issue 41's own document never appears


def test_score_question_handles_an_empty_ranking() -> None:
    row = ridr.score_question("q1", [], "truth.md", "41")

    assert row.truth_rank is None
    assert row.truth_rank_commit_only is None
    assert row.issue_doc_rank is None
    assert row.top1_is_issue_doc is False
    assert row.top1_is_own_issue is False


def test_render_reports_recall_and_own_issue_document_coverage() -> None:
    rows = [
        ridr.score_question("q1", ["truth1.md"], "truth1.md", "1"),
        ridr.score_question(
            "q2", ["2026-01-01-issue-2-x.md", "truth2.md"], "truth2.md", "2",
        ),
    ]

    rendered = ridr.render(rows)

    assert "n=2" in rendered
    assert "commit truth: recall@3=1.000" in rendered
    assert "own issue doc: recall@3=0.500" in rendered
    assert "rank1=1" in rendered
    assert "missing_doc=1" in rendered
    assert "own issue doc ranked above the commit truth: 1" in rendered


def test_render_handles_zero_questions() -> None:
    assert ridr.render([]) == "n=0"
