# Tests for `evals/retrieval/pairs.py` -- finding eval questions that somebody other than the
# answerer wrote.
#
# The property under test is not "does it parse `closes #12`". It is that the three mechanical
# conditions actually exclude the shapes they exist to exclude, and that every exclusion is
# reported with its reason -- a filter that reports only its keepers is asking to be trusted about
# what it dropped, which is the exact thing this module exists to stop the eval from asking for.
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

EVALS_RETRIEVAL = Path(__file__).resolve().parents[1] / "evals" / "retrieval"
if str(EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(EVALS_RETRIEVAL))

from pairs import Commit, Issue, build, corpus_index, independence, parse_closes  # noqa: E402

_WHEN = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _commit(**overrides) -> Commit:
    payload = dict(
        sha="0af3d9c2aaaabbbbccccddddeeeeffff00001111",
        author_name="Pierre-Yves Ricau", author_login="pyricau",
        authored_at=_WHEN, subject="fix the leak", body="closes #2301",
    )
    payload.update(overrides)
    return Commit(**payload)


def _issue(**overrides) -> Issue:
    payload = dict(
        number=2301, title="ToastEventListener leak", author_login="andronaline",
        created_at=_WHEN - timedelta(days=9), is_pull_request=False,
    )
    payload.update(overrides)
    return Issue(**payload)


# --- condition 1: the body names what it closes -------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    ("closes #2301", [2301]),
    ("Fixes: #12", [12]),
    ("resolved #7 and closed #8", [7, 8]),
    ("Closes issue #99", [99]),
    ("closes #5 and later closes #5 again", [5]),          # deduplicated, order preserved
    ("see #4 for context", []),                            # a mention is not a claim to close
    ("", []),
    ("closes the gap described at length in a long paragraph that eventually mentions #4", []),
])
def test_parse_closes(body: str, expected: list[int]) -> None:
    assert parse_closes(body) == expected


# --- condition 2 and 3: the reference is an issue, from someone else, filed first ----------------


def test_a_genuinely_independent_pair_is_accepted() -> None:
    assert independence(_commit(), _issue()) is None


def test_a_pull_request_reference_is_rejected() -> None:
    # A PR is the answer wearing a number, so its title is derived from the fix rather than from
    # somebody's confusion about the problem.
    reason = independence(_commit(), _issue(is_pull_request=True))
    assert reason is not None and "pull request" in reason


def test_the_maintainer_describing_their_own_fix_is_rejected_by_login() -> None:
    reason = independence(_commit(), _issue(author_login="pyricau"))
    assert reason == "issue author is the commit author"


def test_login_comparison_is_case_insensitive() -> None:
    assert independence(_commit(author_login="PyRicau"), _issue(author_login="pyricau")) is not None


def test_an_issue_filed_after_the_commit_cannot_have_prompted_it() -> None:
    """The clause that catches what the author test cannot.

    A maintainer who writes the fix, then opens an issue describing it, passes the author check the
    moment a second person's handle is on the commit. Rare in practice -- over `square/leakcanary`'s
    whole history the author test rejected 123 references and this clause rejected 1 more -- and
    kept because it costs one comparison. Asserted here precisely because a rule that fires once in
    three hundred is the kind somebody deletes as dead weight without checking what it was for.
    """
    later = _issue(created_at=_WHEN + timedelta(hours=1))
    reason = independence(_commit(), later)
    assert reason is not None and "after the commit" in reason


def test_an_issue_filed_at_the_same_instant_is_rejected_too() -> None:
    # Equal timestamps mean the issue did not precede anything. Treated as not-independent because
    # discarding a usable pair is the cheap error and admitting a self-authored one is not.
    assert independence(_commit(), _issue(created_at=_WHEN)) is not None


def test_without_logins_a_name_match_is_treated_as_the_same_person() -> None:
    # The generous direction on purpose: a false "same author" loses a pair, a false "independent"
    # poisons the set.
    reason = independence(
        _commit(author_login=None, author_name="Pierre-Yves Ricau"),
        _issue(author_login="pyricau"),
    )
    assert reason is not None and "name match" in reason


def test_without_logins_a_genuinely_different_name_still_passes() -> None:
    assert independence(
        _commit(author_login=None, author_name="Pierre-Yves Ricau"),
        _issue(author_login="andronaline"),
    ) is None


# --- ground truth is matched by sha, never by rebuilding the filename ---------------------------


def test_corpus_index_maps_sha_to_the_name_ground_truth_records() -> None:
    index = corpus_index([
        "2026-07-25-0af3d9c2-fix-the-leak.md",
        "2026-07-26-1b2c3d4e-discount-the-lexical-leg.md",
        "not-a-corpus-file.txt",
    ])
    assert index["0af3d9c2"] == "2026-07-25-fix-the-leak.md"
    assert index["1b2c3d4e"] == "2026-07-26-discount-the-lexical-leg.md"
    assert len(index) == 2


# --- build: what survives, and what is reported as dropped --------------------------------------


_CORPUS = {"0af3d9c2": "2026-07-25-fix-the-leak.md"}


def test_build_keeps_an_independent_pair_with_the_issue_title_as_the_question() -> None:
    kept, rejected = build([_commit()], {2301: _issue()}, _CORPUS)
    assert rejected == []
    assert len(kept) == 1
    pair = kept[0]
    # The question is the issue title verbatim -- how somebody actually asked, not a paraphrase of
    # the answer written afterwards.
    assert pair.question == "ToastEventListener leak"
    assert pair.ground_truth == "2026-07-25-fix-the-leak.md"
    assert (pair.issue_author, pair.commit_author) == ("andronaline", "pyricau")


def test_build_reports_every_rejection_with_its_reason() -> None:
    commits = [
        _commit(body="closes #1"),                                    # self-authored
        _commit(body="closes #2"),                                    # unresolvable
        _commit(body="closes #3"),                                    # a PR
        _commit(body="closes #4"),                                    # kept
    ]
    issues = {
        1: _issue(number=1, author_login="pyricau"),
        3: _issue(number=3, is_pull_request=True),
        4: _issue(number=4),
    }
    kept, rejected = build(commits, issues, _CORPUS)
    assert [pair.issue_number for pair in kept] == [4]
    reasons = {item.issue_number: item.reason for item in rejected}
    assert reasons[1] == "issue author is the commit author"
    assert reasons[2] == "issue could not be resolved"
    assert "pull request" in reasons[3]


def test_a_commit_absent_from_the_corpus_is_reported_not_paired() -> None:
    # Pairing a question with a document the corpus does not contain would score as a permanent
    # miss and read as a retrieval failure.
    kept, rejected = build([_commit(sha="ffffffffffffffffffffffffffffffffffffffff")],
                           {2301: _issue()}, _CORPUS)
    assert kept == []
    assert rejected[0].reason == "commit is not in the derived corpus"


def test_a_commit_with_no_closing_reference_is_not_a_rejection() -> None:
    # Most commits close nothing. Listing them as rejects would bury the rejections that carry
    # information under thousands that do not.
    kept, rejected = build([_commit(body="a body with no reference at all")], {}, _CORPUS)
    assert kept == [] and rejected == []


def test_one_commit_closing_two_issues_yields_two_pairs() -> None:
    kept, _rejected = build(
        [_commit(body="closes #10 and closes #11")],
        {10: _issue(number=10), 11: _issue(number=11, title="a second report")},
        _CORPUS,
    )
    assert [pair.issue_number for pair in kept] == [10, 11]
    assert kept[1].question == "a second report"
