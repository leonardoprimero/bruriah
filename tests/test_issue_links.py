"""Tests for `bruriah.issue_links.linked_issues`: GitHub's own issue-linking grammar applied to a
commit subject and body.

Cases are grouped by grammar rule (see the module docstring in `bruriah.issue_links` for the
grammar reference and the reasoning behind the two exclusions -- code spans and the squash
suffix). Every case is a pure input/output pair: no git, no network, no filesystem.
"""

from __future__ import annotations

import pytest
from bruriah.issue_links import IssueLink, linked_issues


class TestClosingKeywords:
    @pytest.mark.parametrize(
        "keyword",
        [
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ],
    )
    def test_every_closing_keyword_case_insensitive(self, keyword: str) -> None:
        for variant in (keyword, keyword.upper(), keyword.capitalize()):
            assert linked_issues(f"{variant} #42", "") == (
                IssueLink(number=42, kind="closes", repo=None, keyword=keyword.lower()),
            )

    def test_colon_form(self) -> None:
        assert linked_issues("Fixes: #42", "") == (IssueLink(number=42, kind="closes", repo=None, keyword="fixes"),)

    def test_colon_no_space_form(self) -> None:
        assert linked_issues("Fixes:#42", "") == (IssueLink(number=42, kind="closes", repo=None, keyword="fixes"),)

    def test_keyword_in_body(self) -> None:
        assert linked_issues("subject", "This closes #7 for good.") == (
            IssueLink(number=7, kind="closes", repo=None, keyword="closes"),
        )

    def test_keyword_substring_inside_another_word_is_not_matched(self) -> None:
        assert linked_issues("encloses #5", "") == (IssueLink(number=5, kind="mentions", repo=None, keyword=None),)


class TestCrossRepoAndUrlForms:
    def test_cross_repo_form(self) -> None:
        assert linked_issues("Fixes octocat/Hello-World#123", "") == (
            IssueLink(number=123, kind="closes", repo="octocat/Hello-World", keyword="fixes"),
        )

    def test_issue_url_form(self) -> None:
        assert linked_issues("Closes https://github.com/octocat/Hello-World/issues/123", "") == (
            IssueLink(number=123, kind="closes", repo="octocat/Hello-World", keyword="closes"),
        )

    def test_pull_url_form(self) -> None:
        assert linked_issues("Resolves https://github.com/octocat/Hello-World/pull/9", "") == (
            IssueLink(number=9, kind="closes", repo="octocat/Hello-World", keyword="resolves"),
        )

    def test_bare_cross_repo_mention(self) -> None:
        assert linked_issues("See octocat/Hello-World#5 for context", "") == (
            IssueLink(number=5, kind="mentions", repo="octocat/Hello-World", keyword=None),
        )


class TestSquashSuffix:
    def test_squash_merge_suffix_is_pull_request_self(self) -> None:
        assert linked_issues("Add feature X (#123)", "") == (
            IssueLink(number=123, kind="pull_request_self", repo=None, keyword=None),
        )

    def test_squash_suffix_does_not_also_appear_as_mention(self) -> None:
        result = linked_issues("Add feature X (#123)", "")
        assert len(result) == 1
        assert result[0].kind == "pull_request_self"

    def test_squash_suffix_must_be_at_the_end(self) -> None:
        assert linked_issues("(#123) add feature X", "") == (
            IssueLink(number=123, kind="mentions", repo=None, keyword=None),
        )

    def test_squash_suffix_wins_even_directly_after_a_closing_keyword(self) -> None:
        # Not supported as a closing reference: GitHub's automatic squash suffix always follows
        # the PR title, never a bare keyword, so a keyword sitting directly against the trailing
        # parenthesis is still read as the (unlikely) squash-suffix shape, not as `closes`. See
        # `_squash_suffix` in `bruriah.issue_links` for the reasoning.
        assert linked_issues("Fixes (#123)", "") == (
            IssueLink(number=123, kind="pull_request_self", repo=None, keyword=None),
        )


class TestBareMentions:
    def test_bare_hash_number(self) -> None:
        assert linked_issues("subject", "See #5 for background.") == (
            IssueLink(number=5, kind="mentions", repo=None, keyword=None),
        )

    def test_mention_vs_closes_precedence_same_reference(self) -> None:
        # The same reference appears once as a plain mention and once behind a closing keyword;
        # the overall result keeps `closes`, and there is exactly one link for it.
        result = linked_issues("subject", "See #5 for background. Fixes #5.")
        assert result == (IssueLink(number=5, kind="closes", repo=None, keyword="fixes"),)

    def test_mention_vs_closes_precedence_reverse_order(self) -> None:
        # `closes` wins even when the closing reference appears in the text before the bare
        # mention of the same number.
        result = linked_issues("subject", "Fixes #5. See #5 for background.")
        assert result == (IssueLink(number=5, kind="closes", repo=None, keyword="fixes"),)


class TestExclusions:
    def test_fenced_code_block_is_excluded(self) -> None:
        body = "before\n```\nFixes #5\n```\nafter"
        assert linked_issues("subject", body) == ()

    def test_inline_code_span_is_excluded(self) -> None:
        assert linked_issues("subject", "Use `#5` as a placeholder, not a reference.") == ()

    def test_word_char_before_hash_is_excluded(self) -> None:
        assert linked_issues("Supports C#5 syntax", "") == ()

    def test_keyword_glued_to_hash_with_no_separator_is_excluded(self) -> None:
        # "Fixes#5" has no space or colon between keyword and reference, so it is not read as a
        # closing form; and the hash is directly glued to the word "Fixes", so the same word-char
        # exclusion that skips "issue#5" also skips this -- it is not read as a mention either.
        assert linked_issues("Fixes#5", "") == ()

    def test_word_char_before_hash_is_excluded_no_space(self) -> None:
        assert linked_issues("subject", "see issue#5 here") == ()

    def test_word_char_after_number_is_excluded(self) -> None:
        assert linked_issues("subject", "build #5a failed") == ()

    def test_leading_zero_is_excluded(self) -> None:
        assert linked_issues("subject", "Fixes #007") == ()

    def test_zero_is_not_a_reference(self) -> None:
        assert linked_issues("subject", "Fixes #0") == ()


class TestDedupAndOrdering:
    def test_dedup_by_number_and_repo(self) -> None:
        result = linked_issues("subject", "See #5 and also #5 again.")
        assert result == (IssueLink(number=5, kind="mentions", repo=None, keyword=None),)

    def test_same_number_different_repo_is_not_deduped(self) -> None:
        result = linked_issues("subject", "See octocat/a#5 and octocat/b#5.")
        assert result == (
            IssueLink(number=5, kind="mentions", repo="octocat/a", keyword=None),
            IssueLink(number=5, kind="mentions", repo="octocat/b", keyword=None),
        )

    def test_ordered_by_first_appearance_subject_before_body(self) -> None:
        result = linked_issues("Fixes #9", "Mentions #3 and #7.")
        assert result == (
            IssueLink(number=9, kind="closes", repo=None, keyword="fixes"),
            IssueLink(number=3, kind="mentions", repo=None, keyword=None),
            IssueLink(number=7, kind="mentions", repo=None, keyword=None),
        )

    def test_ordered_within_body_by_first_appearance(self) -> None:
        result = linked_issues("subject", "First #7, then #3, then #7 again.")
        assert result == (
            IssueLink(number=7, kind="mentions", repo=None, keyword=None),
            IssueLink(number=3, kind="mentions", repo=None, keyword=None),
        )


class TestEmptyInputs:
    def test_empty_subject_and_body(self) -> None:
        assert linked_issues("", "") == ()

    def test_subject_with_no_references(self) -> None:
        assert linked_issues("Refactor the parser", "") == ()

    def test_body_with_no_references(self) -> None:
        assert linked_issues("subject", "No references here at all.") == ()
