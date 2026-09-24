from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bruriah.github import (
    GitHubError,
    PRContext,
    _github_api,
    detect_pr_context,
    dismiss_previous_reviews,
    post_review,
)
from bruriah.review import ReviewComment, ReviewResult


# ---------------------------------------------------------------------------
# PRContext
# ---------------------------------------------------------------------------


class TestPRContext:
    def test_frozen_dataclass(self) -> None:
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc123")
        assert ctx.owner == "leo"
        assert ctx.repo == "bruriah"
        assert ctx.pr_number == 42
        with pytest.raises(AttributeError):
            ctx.owner = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# detect_pr_context
# ---------------------------------------------------------------------------


class TestDetectPRContext:
    def test_returns_none_without_env_vars(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert detect_pr_context() is None

    def test_returns_none_without_event_path(self) -> None:
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "leo/bruriah"}, clear=True):
            assert detect_pr_context() is None

    def test_returns_none_without_github_repository(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text('{"pull_request": {"number": 1, "base": {"ref": "main"}, "head": {"sha": "abc"}}}')
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}, clear=True):
            assert detect_pr_context() is None

    def test_returns_none_for_non_pr_event(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text('{"action": "push"}')
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": "leo/bruriah",
            },
            clear=True,
        ):
            assert detect_pr_context() is None

    def test_returns_none_for_missing_file(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": "/nonexistent/path.json",
                "GITHUB_REPOSITORY": "leo/bruriah",
            },
            clear=True,
        ):
            assert detect_pr_context() is None

    def test_returns_none_for_invalid_json(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text("not json{{{")
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": "leo/bruriah",
            },
            clear=True,
        ):
            assert detect_pr_context() is None

    def test_returns_none_for_invalid_repository_format(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text('{"pull_request": {"number": 1, "base": {"ref": "main"}, "head": {"sha": "abc"}}}')
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": "noslash",
            },
            clear=True,
        ):
            assert detect_pr_context() is None

    def test_returns_context_for_valid_pr_event(self, tmp_path: Path) -> None:
        event_data = {
            "pull_request": {
                "number": 42,
                "base": {"ref": "main"},
                "head": {"sha": "abc123def456"},
            }
        }
        event = tmp_path / "event.json"
        event.write_text(json.dumps(event_data))
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": "leonardoprimero/bruriah",
            },
            clear=True,
        ):
            ctx = detect_pr_context()
            assert ctx is not None
            assert ctx.owner == "leonardoprimero"
            assert ctx.repo == "bruriah"
            assert ctx.pr_number == 42
            assert ctx.base_ref == "main"
            assert ctx.head_sha == "abc123def456"

    def test_returns_none_for_incomplete_pr_data(self, tmp_path: Path) -> None:
        event_data = {
            "pull_request": {
                "number": 42,
                # Missing base and head
            }
        }
        event = tmp_path / "event.json"
        event.write_text(json.dumps(event_data))
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": "leo/bruriah",
            },
            clear=True,
        ):
            assert detect_pr_context() is None


# ---------------------------------------------------------------------------
# GitHubError
# ---------------------------------------------------------------------------


class TestGitHubError:
    def test_code_only(self) -> None:
        err = GitHubError("github_api_403")
        assert err.code == "github_api_403"
        assert err.detail == ""
        assert str(err) == "github_api_403"

    def test_code_and_detail(self) -> None:
        err = GitHubError("github_api_422", "Unprocessable Entity")
        assert err.code == "github_api_422"
        assert err.detail == "Unprocessable Entity"
        assert "422" in str(err)
        assert "Unprocessable Entity" in str(err)


# ---------------------------------------------------------------------------
# post_review
# ---------------------------------------------------------------------------


class TestPostReview:
    def _make_review(self, *, with_comments: bool = True) -> ReviewResult:
        comments = ()
        if with_comments:
            comments = (
                ReviewComment(path="src/auth.py", body="⚠️ Drift!", line=42, side="RIGHT"),
                ReviewComment(path="src/config.py", body="⚠️ File-level drift!"),
            )
        return ReviewResult(
            body="## 🏛️ Bruriah Review\nClean.",
            comments=comments,
            event="COMMENT",
            has_drift=with_comments,
            inspected_count=2,
            stale_count=1 if with_comments else 0,
            clean_count=1,
            unindexed_count=0,
        )

    @patch("bruriah.github._github_api")
    def test_posts_review_and_returns_url(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {"html_url": "https://github.com/leo/bruriah/pull/42#review-123"}
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc123")
        review = self._make_review()

        url = post_review("test-token", ctx, review)
        assert url == "https://github.com/leo/bruriah/pull/42#review-123"

        mock_api.assert_called_once()
        call_args = mock_api.call_args
        assert call_args[0][0] == "POST"  # method
        assert "/pulls/42/reviews" in call_args[0][1]  # url
        assert call_args[0][2] == "test-token"  # token

        payload = call_args[0][3]
        assert payload["body"] == review.body
        assert payload["event"] == "COMMENT"
        assert payload["commit_id"] == "abc123"
        assert len(payload["comments"]) == 2

        # Line-level comment
        assert payload["comments"][0]["path"] == "src/auth.py"
        assert payload["comments"][0]["line"] == 42
        assert payload["comments"][0]["side"] == "RIGHT"

        # File-level comment (no line)
        assert "line" not in payload["comments"][1]

    @patch("bruriah.github._github_api")
    def test_posts_review_without_comments(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {"html_url": "https://example.com/review"}
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=1, base_ref="main", head_sha="def")
        review = self._make_review(with_comments=False)

        url = post_review("token", ctx, review)
        assert url == "https://example.com/review"
        payload = mock_api.call_args[0][3]
        assert payload["comments"] == []

    @patch("bruriah.github._github_api")
    def test_raises_github_error_on_failure(self, mock_api: MagicMock) -> None:
        mock_api.side_effect = GitHubError("github_api_422", "Validation failed")
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=1, base_ref="main", head_sha="def")
        review = self._make_review()

        with pytest.raises(GitHubError, match="github_api_422"):
            post_review("token", ctx, review)


# ---------------------------------------------------------------------------
# dismiss_previous_reviews
# ---------------------------------------------------------------------------


class TestDismissPreviousReviews:
    @patch("bruriah.github._github_api")
    def test_dismisses_matching_reviews(self, mock_api: MagicMock) -> None:
        mock_api.side_effect = [
            # GET reviews
            [
                {
                    "id": 100,
                    "user": {"login": "github-actions[bot]"},
                    "body": "## 🏛️ Bruriah Architectural Review\nStuff.",
                    "state": "COMMENTED",
                },
                {
                    "id": 200,
                    "user": {"login": "human-reviewer"},
                    "body": "LGTM",
                    "state": "APPROVED",
                },
            ],
            # PUT dismiss review 100
            {},
        ]
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc")
        count = dismiss_previous_reviews("token", ctx)
        assert count == 1
        assert mock_api.call_count == 2

    @patch("bruriah.github._github_api")
    def test_skips_already_dismissed(self, mock_api: MagicMock) -> None:
        mock_api.return_value = [
            {
                "id": 100,
                "user": {"login": "github-actions[bot]"},
                "body": "## 🏛️ Bruriah Architectural Review\nStuff.",
                "state": "DISMISSED",
            },
        ]
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc")
        count = dismiss_previous_reviews("token", ctx)
        assert count == 0

    @patch("bruriah.github._github_api")
    def test_returns_zero_on_api_failure(self, mock_api: MagicMock) -> None:
        mock_api.side_effect = GitHubError("github_api_500", "Internal Server Error")
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc")
        count = dismiss_previous_reviews("token", ctx)
        assert count == 0

    @patch("bruriah.github._github_api")
    def test_handles_non_list_response(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {"error": "unexpected"}
        ctx = PRContext(owner="leo", repo="bruriah", pr_number=42, base_ref="main", head_sha="abc")
        count = dismiss_previous_reviews("token", ctx)
        assert count == 0
