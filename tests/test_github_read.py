"""Tests for `bruriah.github_read`: the response cache, bounded retry, pagination, and the four
GitHub read primitives (`get_issue`, `get_issue_timeline`, `get_pull`, `get_issue_comments`).

Every network path in this module goes through an injectable `Transport` callable, so these tests
never open a socket: a cache hit is proven by injecting a transport that raises `AssertionError`
if it is ever called, and retry/backoff/rate-limit behavior is proven with injected `clock`/`sleep`
callables instead of real timing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bruriah.github import GitHubError
from bruriah.github_read import (
    GitHubNotFoundError,
    GitHubOfflineError,
    GitHubRateLimitedError,
    ResponseCache,
    _RawResponse,
    get_issue,
    get_issue_comments,
    get_issue_timeline,
    get_pull,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _never_called(*_args: Any, **_kwargs: Any) -> _RawResponse:
    raise AssertionError("transport must not be called")


def _clock_seq(*values: float) -> Any:
    it = iter(values)

    def _clock() -> float:
        return next(it)

    return _clock


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _ScriptedTransport:
    """Returns one `_RawResponse` per call, in order; records every (method, url, token)."""

    def __init__(self, responses: list[_RawResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, method: str, url: str, token: str | None) -> _RawResponse:
        self.calls.append((method, url, token))
        return self._responses.pop(0)


def _json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> _RawResponse:
    return _RawResponse(status=status, headers=headers or {}, body=json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# ResponseCache
# ---------------------------------------------------------------------------


class TestResponseCache:
    def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        assert cache.get("/repos/acme/widget/issues/41") is None

    def test_set_then_get_roundtrips(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        payload = {"number": 41, "state": "closed"}
        cache.set("/repos/acme/widget/issues/41", payload)
        assert cache.get("/repos/acme/widget/issues/41") == payload

    def test_sanitizes_endpoint_path_to_a_single_filename(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/issues/41", {"a": 1})
        files = list(tmp_path.glob("*"))
        assert len(files) == 1
        assert files[0].is_file()

    def test_distinct_paths_do_not_collide(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/issues/41", {"which": "issue"})
        cache.set("/repos/acme/widget/pulls/41", {"which": "pull"})
        assert cache.get("/repos/acme/widget/issues/41") == {"which": "issue"}
        assert cache.get("/repos/acme/widget/pulls/41") == {"which": "pull"}


# ---------------------------------------------------------------------------
# Cache-hit / offline behavior shared by every read primitive
# ---------------------------------------------------------------------------


class TestCacheAndOffline:
    def test_cache_hit_performs_no_network(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/issues/41", _load("issue-41.json"))
        result = get_issue("acme", "widget", 41, cache=cache, transport=_never_called)
        assert result["number"] == 41

    def test_cache_miss_with_network_disabled_raises_typed_error(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        with pytest.raises(GitHubOfflineError):
            get_issue(
                "acme", "widget", 41, cache=cache, network_enabled=False, transport=_never_called,
            )

    def test_offline_error_is_a_github_error(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        with pytest.raises(GitHubError):
            get_issue(
                "acme", "widget", 41, cache=cache, network_enabled=False, transport=_never_called,
            )

    def test_successful_fetch_populates_the_cache(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(200, _load("issue-41.json"))])
        result = get_issue("acme", "widget", 41, cache=cache, transport=transport)
        assert result["number"] == 41
        assert len(transport.calls) == 1
        # A second call must not touch the network again.
        result_again = get_issue("acme", "widget", 41, cache=cache, transport=_never_called)
        assert result_again["number"] == 41


# ---------------------------------------------------------------------------
# 404 handling
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_404_raises_without_retry(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(404, {"message": "Not Found"})])
        with pytest.raises(GitHubNotFoundError):
            get_issue("acme", "widget", 999, cache=cache, transport=transport)
        assert len(transport.calls) == 1

    def test_not_found_error_is_a_github_error(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(404, {"message": "Not Found"})])
        with pytest.raises(GitHubError):
            get_pull("acme", "widget", 999, cache=cache, transport=transport)

    def test_404_never_populates_the_cache(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(404, {"message": "Not Found"})])
        with pytest.raises(GitHubNotFoundError):
            get_issue("acme", "widget", 999, cache=cache, transport=transport)
        assert cache.get("/repos/acme/widget/issues/999") is None


# ---------------------------------------------------------------------------
# Retry on 5xx
# ---------------------------------------------------------------------------


class TestServerErrorRetry:
    def test_retries_then_succeeds(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(500, {"message": "boom"}),
            _json_response(502, {"message": "boom again"}),
            _json_response(200, _load("issue-41.json")),
        ])
        sleep = _RecordingSleep()
        result = get_issue(
            "acme", "widget", 41, cache=cache, transport=transport, sleep=sleep, clock=_clock_seq(0.0, 0.0, 0.0),
        )
        assert result["number"] == 41
        assert len(transport.calls) == 3
        assert len(sleep.calls) == 2
        # Exponential backoff: each wait is strictly longer than the last.
        assert sleep.calls[1] > sleep.calls[0]

    def test_exhausts_retries_and_raises(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(500, {"message": "boom"}),
            _json_response(500, {"message": "boom"}),
            _json_response(500, {"message": "boom"}),
        ])
        sleep = _RecordingSleep()
        with pytest.raises(GitHubError):
            get_issue("acme", "widget", 41, cache=cache, transport=transport, sleep=sleep, clock=_clock_seq(0.0, 0.0, 0.0))
        assert len(transport.calls) == 3

    def test_never_retries_a_plain_4xx(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(422, {"message": "Unprocessable"})])
        with pytest.raises(GitHubError):
            get_issue("acme", "widget", 41, cache=cache, transport=transport)
        assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Default transport timeout (R4-001): a stalled connection must never block forever.
# ---------------------------------------------------------------------------


class TestDefaultTransportTimeout:
    def test_urlopen_is_called_with_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bruriah.github_read as github_read

        captured: dict[str, Any] = {}

        class _FakeResponse:
            status = 200
            headers: dict[str, str] = {}

            def read(self) -> bytes:
                return b"{}"

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

        def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(github_read.urllib.request, "urlopen", _fake_urlopen)
        github_read._default_transport(
            "GET", "https://api.github.com/repos/acme/widget/issues/41", None,
        )
        assert captured["timeout"] == github_read._DEFAULT_TIMEOUT_SECONDS

    def test_a_stalled_connection_raises_a_typed_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bruriah.github_read as github_read

        def _fake_urlopen(request: Any, timeout: float | None = None) -> Any:
            raise TimeoutError("timed out")

        monkeypatch.setattr(github_read.urllib.request, "urlopen", _fake_urlopen)
        with pytest.raises(GitHubError) as excinfo:
            github_read._default_transport(
                "GET", "https://api.github.com/repos/acme/widget/issues/41", None,
            )
        assert excinfo.value.code == "github_timeout"

    def test_a_urlerror_wrapping_a_timeout_also_raises_the_typed_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error

        import bruriah.github_read as github_read

        def _fake_urlopen(request: Any, timeout: float | None = None) -> Any:
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr(github_read.urllib.request, "urlopen", _fake_urlopen)
        with pytest.raises(GitHubError) as excinfo:
            github_read._default_transport("GET", "https://api.github.com/x", None)
        assert excinfo.value.code == "github_timeout"


# ---------------------------------------------------------------------------
# Rate-limit handling (403 / 429)
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_403_with_remaining_zero_sleeps_until_reset(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(
                403, {"message": "rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"},
            ),
            _json_response(200, _load("issue-41.json")),
        ])
        sleep = _RecordingSleep()
        result = get_issue(
            "acme", "widget", 41, cache=cache, transport=transport, sleep=sleep, clock=_clock_seq(940.0, 940.0),
        )
        assert result["number"] == 41
        assert sleep.calls == [60.0]

    def test_429_with_retry_after_sleeps_that_many_seconds(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(429, {"message": "rate limited"}, headers={"Retry-After": "30"}),
            _json_response(200, _load("issue-41.json")),
        ])
        sleep = _RecordingSleep()
        result = get_issue("acme", "widget", 41, cache=cache, transport=transport, sleep=sleep, clock=_clock_seq(0.0, 0.0))
        assert result["number"] == 41
        assert sleep.calls == [30.0]

    def test_403_without_rate_limit_headers_is_not_retried_as_rate_limit(self, tmp_path: Path) -> None:
        # A plain 403 (no permission) carries neither header and must not be treated as
        # rate-limited retry material -- but it is also not a 5xx, so it still fails immediately.
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(403, {"message": "Forbidden"})])
        with pytest.raises(GitHubError):
            get_issue("acme", "widget", 41, cache=cache, transport=transport)
        assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Rate limit window closed (R4-002): a reset far in the future must never be slept through, and a
# request must never be retried a blind third time after the one rate-limit sleep is spent.
# ---------------------------------------------------------------------------


class TestRateLimitWindowClosed:
    def test_far_reset_raises_immediately_without_sleeping(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(
                403, {"message": "rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"},
            ),
        ])
        sleep = _RecordingSleep()
        with pytest.raises(GitHubRateLimitedError) as excinfo:
            get_issue(
                "acme", "widget", 41, cache=cache, transport=transport, sleep=sleep,
                clock=_clock_seq(0.0, 0.0),
            )
        assert sleep.calls == []
        assert len(transport.calls) == 1
        assert excinfo.value.reset_at == pytest.approx(3600.0)

    def test_rate_limited_error_is_a_github_error(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(
                403, {"message": "rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"},
            ),
        ])
        with pytest.raises(GitHubError):
            get_issue(
                "acme", "widget", 41, cache=cache, transport=transport,
                clock=_clock_seq(0.0, 0.0),
            )

    def test_near_reset_sleeps_once_for_the_full_wait_then_retries(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(
                403, {"message": "rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "120"},
            ),
            _json_response(200, _load("issue-41.json")),
        ])
        sleep = _RecordingSleep()
        result = get_issue(
            "acme", "widget", 41, cache=cache, transport=transport, sleep=sleep,
            clock=_clock_seq(0.0, 0.0),
        )
        assert result["number"] == 41
        assert sleep.calls == [120.0]
        assert len(transport.calls) == 2

    def test_rate_limited_again_after_the_one_sleep_raises_without_a_third_attempt(
        self, tmp_path: Path
    ) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([
            _json_response(
                403, {"message": "rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "10"},
            ),
            _json_response(
                403, {"message": "still rate limited"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "10"},
            ),
        ])
        sleep = _RecordingSleep()
        with pytest.raises(GitHubRateLimitedError):
            get_issue(
                "acme", "widget", 41, cache=cache, transport=transport, sleep=sleep,
                clock=_clock_seq(0.0, 0.0, 0.0),
            )
        assert len(transport.calls) == 2
        assert sleep.calls == [10.0]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_follows_link_header_and_concatenates_pages(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        page1 = [{"body": "first"}]
        page2 = [{"body": "second"}]
        next_url = "https://api.github.com/repositories/1/issues/41/comments?page=2"
        transport = _ScriptedTransport([
            _json_response(200, page1, headers={"Link": f'<{next_url}>; rel="next"'}),
            _json_response(200, page2),
        ])
        result = get_issue_comments("acme", "widget", 41, cache=cache, transport=transport)
        assert result == [{"body": "first"}, {"body": "second"}]
        assert len(transport.calls) == 2
        assert transport.calls[1][1] == next_url

    def test_single_page_without_link_header_stops(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        transport = _ScriptedTransport([_json_response(200, [{"body": "only"}])])
        result = get_issue_comments("acme", "widget", 41, cache=cache, transport=transport)
        assert result == [{"body": "only"}]
        assert len(transport.calls) == 1

    def test_pagination_is_bounded_by_a_page_cap(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        from bruriah.github_read import _MAX_PAGES

        class _InfiniteTransport:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, method: str, url: str, token: str | None) -> _RawResponse:
                self.calls += 1
                next_url = f"https://api.github.com/x?page={self.calls + 1}"
                return _json_response(200, [{"n": self.calls}], headers={"Link": f'<{next_url}>; rel="next"'})

        transport = _InfiniteTransport()
        result = get_issue_comments("acme", "widget", 41, cache=cache, transport=transport)
        assert transport.calls == _MAX_PAGES
        assert len(result) == _MAX_PAGES

    def test_paginated_result_is_cached_as_the_concatenated_list(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        next_url = "https://api.github.com/repositories/1/issues/41/comments?page=2"
        transport = _ScriptedTransport([
            _json_response(200, [{"body": "first"}], headers={"Link": f'<{next_url}>; rel="next"'}),
            _json_response(200, [{"body": "second"}]),
        ])
        get_issue_comments("acme", "widget", 41, cache=cache, transport=transport)
        cached = cache.get("/repos/acme/widget/issues/41/comments")
        assert cached == [{"body": "first"}, {"body": "second"}]
        # Now fully served from cache, no network at all.
        result = get_issue_comments("acme", "widget", 41, cache=cache, transport=_never_called)
        assert result == [{"body": "first"}, {"body": "second"}]


# ---------------------------------------------------------------------------
# get_issue_timeline
# ---------------------------------------------------------------------------


class TestIssueTimeline:
    def test_stores_and_returns_the_raw_page(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        events = _load("issue-41-timeline.json")
        transport = _ScriptedTransport([_json_response(200, events)])
        result = get_issue_timeline("acme", "widget", 41, cache=cache, transport=transport)
        assert result == events
        cross_refs = [e for e in result if e["event"] == "cross-referenced"]
        assert len(cross_refs) == 2
        numbers = {e["source"]["issue"]["number"] for e in cross_refs}
        assert numbers == {50, 47}


# ---------------------------------------------------------------------------
# get_pull
# ---------------------------------------------------------------------------


class TestGetPull:
    def test_merged_pull_from_fixture(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/pulls/50", _load("pull-50.json"))
        pull = get_pull("acme", "widget", 50, cache=cache, transport=_never_called)
        assert pull["merged"] is True
        assert pull["merged_at"] == "2026-06-09T12:00:00Z"

    def test_unmerged_closed_pull_from_fixture(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/pulls/47", _load("pull-47.json"))
        pull = get_pull("acme", "widget", 47, cache=cache, transport=_never_called)
        assert pull["state"] == "closed"
        assert pull["merged"] is False
        assert pull["merged_at"] is None


# ---------------------------------------------------------------------------
# Fixture sanity (also exercises get_issue_comments over recorded data)
# ---------------------------------------------------------------------------


class TestFixtures:
    def test_issue_41_is_closed_completed(self) -> None:
        issue = _load("issue-41.json")
        assert issue["number"] == 41
        assert issue["state"] == "closed"
        assert issue["state_reason"] == "completed"
        assert "Premise:" in issue["body"]

    def test_issue_39_is_not_planned_and_references_41(self) -> None:
        issue = _load("issue-39.json")
        assert issue["number"] == 39
        assert issue["state_reason"] == "not_planned"
        assert "#41" in issue["body"]

    def test_pull_47_closing_comment_is_recorded(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        cache.set("/repos/acme/widget/issues/47/comments", _load("pull-47-comments.json"))
        comments = get_issue_comments("acme", "widget", 47, cache=cache, transport=_never_called)
        assert len(comments) == 1
        assert "file-lock approach" in comments[0]["body"]
