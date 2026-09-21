"""GitHub READ primitives for issue/PR ingestion (`bruriah corpus --github`, T3).

A sibling of `github.py` rather than an addition to it: `github.py` posts and dismisses PR
reviews (a write-side concern with its own, already-tested `_github_api`/`GitHubError` pair); this
module only reads issues, pulls, timelines, and comments, and needs machinery `_github_api` does
not expose -- response headers (for rate-limit detection and `Link` pagination) and a swappable
transport (so tests never open a socket). Splitting keeps `tests/test_github.py` and
`post_review`/`dismiss_previous_reviews` completely untouched, and keeps each module under the
project's usual size.

Design, matching `fetch.py`'s conventions where they apply:

- **Cache before network.** `ResponseCache` is a flat directory keyed by the GitHub API path
  (sanitized to a filename); every read primitive checks it before making any request. With
  `network_enabled=False` and a cache miss, `GitHubOfflineError` is raised and no socket is ever
  opened -- the structural switch `fetch.py` uses, applied here without `fetch.py`'s SSRF
  machinery (this module only ever talks to `api.github.com`, never a caller-supplied host).
- **Token is a parameter, never an environment read.** Same rule `post_review` already follows;
  the CLI (T3) resolves `--github-token-env` and passes the value in.
- **Bounded retry.** Up to `_MAX_ATTEMPTS` (3) attempts per request, exponential backoff on 5xx,
  and on 403/429 that carry `X-RateLimit-Remaining: 0` or `Retry-After` -- sleeping until
  `X-RateLimit-Reset` (capped at `_MAX_RATE_LIMIT_SLEEP_SECONDS`) rather than blind backoff, since
  GitHub tells us exactly when the window reopens. A 404 is never retried: it raises
  `GitHubNotFoundError` immediately so the document builder can skip-and-warn instead of treating a
  missing issue as a transient failure. `clock`/`sleep` are injectable so tests never sleep for
  real.
- **Bounded pagination.** `get_issue_timeline` and `get_issue_comments` follow the `Link: rel=
  "next"` header, concatenating pages into one list, capped at `_MAX_PAGES` (20) requests -- a
  "sane page cap" per the task: at 100 items/page that is 2,000 items per issue thread, far beyond
  any real issue or PR this project ingests, and a hostile or looping `Link` header cannot pin a
  build in an infinite fetch loop. Reaching the cap stops pagination silently (the caller gets
  whatever was fetched) rather than raising, because a very long but legitimate thread should not
  fail a corpus build over a size heuristic.
- **Timeline events are stored raw.** Only `cross-referenced` events matter to the document
  builder, but filtering is the builder's job (T3), not the fetcher's -- `get_issue_timeline`
  returns the full recorded page exactly as GitHub sent it.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .github import GitHubError

_API_ROOT = "https://api.github.com"
_MAX_ATTEMPTS = 3
_MAX_PAGES = 20
_BASE_BACKOFF_SECONDS = 1.0
_MAX_RATE_LIMIT_SLEEP_SECONDS = 300.0
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubNotFoundError(GitHubError):
    """GitHub returned 404 for `path`. Never retried -- the document builder (T3) is expected to
    skip the issue/PR and warn, not treat a missing resource as a transient failure."""

    def __init__(self, path: str) -> None:
        super().__init__("github_not_found", path)
        self.path = path


class GitHubOfflineError(GitHubError):
    """`network_enabled=False` and `ResponseCache` has no entry for `path`. No socket is ever
    opened on this path; see the module docstring."""

    def __init__(self, path: str) -> None:
        super().__init__("github_offline_cache_miss", path)
        self.path = path


@dataclass(frozen=True)
class _RawResponse:
    """One HTTP response, exactly as needed for retry/pagination decisions: status code, headers
    (case-sensitivity handled by `_header`, since real servers and test fixtures disagree on
    casing), and the raw, undecoded body."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, "str | None"], _RawResponse]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _default_transport(method: str, url: str, token: str | None) -> _RawResponse:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bruriah-bot",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return _RawResponse(
                status=response.status, headers=dict(response.headers.items()), body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        response_headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return _RawResponse(status=exc.code, headers=response_headers, body=body)
    except urllib.error.URLError as exc:
        raise GitHubError("github_network_error", str(exc.reason)) from exc


class ResponseCache:
    """Flat, TTL-free cache of raw GitHub API responses, rooted at `root`. Keyed by the endpoint
    path (e.g. `/repos/acme/widget/issues/41`), sanitized to a single filename -- reproducibility
    for tests and offline builds, not a production HTTP cache (no ETags, no expiry)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path_for(self, endpoint_path: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", endpoint_path.strip("/")) or "root"
        return self.root / f"{safe}.json"

    def get(self, endpoint_path: str) -> Any | None:
        path = self._path_for(endpoint_path)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, endpoint_path: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(endpoint_path)
        path.write_text(json.dumps(value), encoding="utf-8")


def _error_detail(response: _RawResponse) -> str:
    try:
        return response.body.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _is_rate_limited(response: _RawResponse) -> bool:
    if response.status not in (403, 429):
        return False
    remaining = _header(response.headers, "X-RateLimit-Remaining")
    return remaining == "0" or _header(response.headers, "Retry-After") is not None


def _rate_limit_delay(response: _RawResponse, now: float) -> float:
    reset = _header(response.headers, "X-RateLimit-Reset")
    if reset is not None:
        try:
            delay = float(reset) - now
        except ValueError:
            delay = _BASE_BACKOFF_SECONDS
    else:
        retry_after = _header(response.headers, "Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else _BASE_BACKOFF_SECONDS
        except ValueError:
            delay = _BASE_BACKOFF_SECONDS
    return max(0.0, min(delay, _MAX_RATE_LIMIT_SLEEP_SECONDS))


def _request_with_retry(
    method: str,
    url: str,
    token: str | None,
    *,
    transport: Transport,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> _RawResponse:
    """One GitHub request, retried up to `_MAX_ATTEMPTS` times on 5xx and on rate-limited 403/429.
    A 404 raises immediately (never retried); any other non-2xx status raises immediately too."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = transport(method, url, token)
        if 200 <= response.status < 300:
            return response
        if response.status == 404:
            raise GitHubNotFoundError(url)

        retryable = response.status >= 500 or _is_rate_limited(response)
        if not retryable or attempt == _MAX_ATTEMPTS:
            raise GitHubError(f"github_api_{response.status}", _error_detail(response))

        if _is_rate_limited(response):
            sleep(_rate_limit_delay(response, clock()))
        else:
            sleep(_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    # Defensive: every branch above returns or raises, so this is unreachable in practice.
    raise GitHubError("github_retry_exhausted", url)


def _next_link(headers: Mapping[str, str]) -> str | None:
    link = _header(headers, "Link")
    if not link:
        return None
    match = _LINK_NEXT_RE.search(link)
    return match.group(1) if match else None


def _fetch_all_pages(
    path: str,
    *,
    token: str | None,
    transport: Transport,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[Any]:
    items: list[Any] = []
    url = f"{_API_ROOT}{path}"
    for _ in range(_MAX_PAGES):
        response = _request_with_retry("GET", url, token, transport=transport, clock=clock, sleep=sleep)
        page = json.loads(response.body.decode("utf-8"))
        if isinstance(page, list):
            items.extend(page)
        next_url = _next_link(response.headers)
        if not next_url:
            break
        url = next_url
    return items


def _get_single(
    path: str,
    *,
    cache: ResponseCache,
    token: str | None,
    network_enabled: bool,
    transport: Transport,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    cached = cache.get(path)
    if cached is not None:
        return cast(dict[str, Any], cached)
    if not network_enabled:
        raise GitHubOfflineError(path)
    response = _request_with_retry(
        "GET", f"{_API_ROOT}{path}", token, transport=transport, clock=clock, sleep=sleep,
    )
    data = json.loads(response.body.decode("utf-8"))
    cache.set(path, data)
    return cast(dict[str, Any], data)


def _get_paginated(
    path: str,
    *,
    cache: ResponseCache,
    token: str | None,
    network_enabled: bool,
    transport: Transport,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[Any]:
    cached = cache.get(path)
    if cached is not None:
        return cast(list[Any], cached)
    if not network_enabled:
        raise GitHubOfflineError(path)
    items = _fetch_all_pages(path, token=token, transport=transport, clock=clock, sleep=sleep)
    cache.set(path, items)
    return items


def get_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    cache: ResponseCache,
    token: str | None = None,
    network_enabled: bool = True,
    transport: Transport = _default_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """`GET /repos/{owner}/{repo}/issues/{number}`. Cache-then-network; see module docstring."""
    path = f"/repos/{owner}/{repo}/issues/{number}"
    return _get_single(
        path, cache=cache, token=token, network_enabled=network_enabled, transport=transport,
        clock=clock, sleep=sleep,
    )


def get_pull(
    owner: str,
    repo: str,
    number: int,
    *,
    cache: ResponseCache,
    token: str | None = None,
    network_enabled: bool = True,
    transport: Transport = _default_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """`GET /repos/{owner}/{repo}/pulls/{number}`. Cache-then-network; see module docstring."""
    path = f"/repos/{owner}/{repo}/pulls/{number}"
    return _get_single(
        path, cache=cache, token=token, network_enabled=network_enabled, transport=transport,
        clock=clock, sleep=sleep,
    )


def get_issue_timeline(
    owner: str,
    repo: str,
    number: int,
    *,
    cache: ResponseCache,
    token: str | None = None,
    network_enabled: bool = True,
    transport: Transport = _default_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Any]:
    """`GET /repos/{owner}/{repo}/issues/{number}/timeline`, paginated and concatenated. Only
    `cross-referenced` events matter to callers, but the raw page is returned and cached
    unfiltered -- filtering is the document builder's job (T3), not this fetcher's."""
    path = f"/repos/{owner}/{repo}/issues/{number}/timeline"
    return _get_paginated(
        path, cache=cache, token=token, network_enabled=network_enabled, transport=transport,
        clock=clock, sleep=sleep,
    )


def get_issue_comments(
    owner: str,
    repo: str,
    number: int,
    *,
    cache: ResponseCache,
    token: str | None = None,
    network_enabled: bool = True,
    transport: Transport = _default_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Any]:
    """`GET /repos/{owner}/{repo}/issues/{number}/comments`, paginated and concatenated. GitHub
    serves pull request comments through this same endpoint, so this also covers a PR's closing
    comment (see `tests/fixtures/github/pull-47-comments.json`)."""
    path = f"/repos/{owner}/{repo}/issues/{number}/comments"
    return _get_paginated(
        path, cache=cache, token=token, network_enabled=network_enabled, transport=transport,
        clock=clock, sleep=sleep,
    )


__all__ = [
    "GitHubNotFoundError",
    "GitHubOfflineError",
    "ResponseCache",
    "Transport",
    "get_issue",
    "get_issue_comments",
    "get_issue_timeline",
    "get_pull",
]
