from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .review import ReviewResult


class GitHubError(Exception):
    """Raised when a GitHub API call fails."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PRContext:
    """Pull request context extracted from GitHub Actions environment."""

    owner: str
    repo: str
    pr_number: int
    base_ref: str
    head_sha: str


def detect_pr_context() -> PRContext | None:
    """
    Detects if the code is running in a GitHub Actions PR context.

    Returns:
        PRContext if in a PR environment, otherwise None.
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repo_env = os.environ.get("GITHUB_REPOSITORY")

    if not event_path or not repo_env:
        return None

    if "/" not in repo_env:
        return None

    owner, repo = repo_env.split("/", 1)

    try:
        with Path(event_path).open(encoding="utf-8") as f:
            event_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    # We expect a pull_request event
    pr_data = event_data.get("pull_request")
    if not pr_data:
        return None

    try:
        pr_number = pr_data["number"]
        base_ref = pr_data["base"]["ref"]
        head_sha = pr_data["head"]["sha"]

        return PRContext(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            base_ref=base_ref,
            head_sha=head_sha,
        )
    except (KeyError, TypeError):
        return None


def _github_api(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    """
    Internal helper to make GitHub API calls using only the standard library.

    Args:
        method: HTTP method (GET, POST, PUT, etc.)
        url: The full API URL.
        token: GitHub personal access token or GITHUB_TOKEN.
        body: Optional dictionary payload for POST/PUT requests.

    Returns:
        Parsed JSON response from the API.

    Raises:
        GitHubError: On network or HTTP errors.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bruriah-bot",
    }

    data = None
    if body is not None:
        try:
            data = json.dumps(body).encode("utf-8")
        except TypeError as e:
            raise GitHubError("github_json_encode_error", str(e))
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as response:
            resp_body = response.read().decode("utf-8")
            if resp_body:
                return json.loads(resp_body)
            return {}
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except Exception:
            err_body = "Could not read error body."
        raise GitHubError(f"github_api_{exc.code}", err_body)
    except urllib.error.URLError as exc:
        raise GitHubError("github_network_error", str(exc.reason))


def post_review(token: str, context: PRContext, review: ReviewResult) -> str:
    """
    Posts a review to the pull request.

    Args:
        token: GitHub token.
        context: PRContext containing owner, repo, etc.
        review: ReviewResult containing the overall review and file comments.

    Returns:
        The URL of the created review.
    """
    url = f"https://api.github.com/repos/{context.owner}/{context.repo}/pulls/{context.pr_number}/reviews"

    comments = []
    for comment in review.comments:
        c: dict[str, Any] = {
            "path": comment.path,
            "body": comment.body,
        }
        if getattr(comment, "line", None) is not None:
            c["line"] = comment.line
            if getattr(comment, "side", None) is not None:
                c["side"] = comment.side
        comments.append(c)

    payload = {
        "body": review.body,
        "event": review.event,
        "commit_id": context.head_sha,
        "comments": comments,
    }

    resp = _github_api("POST", url, token, payload)
    if isinstance(resp, dict):
        return str(resp.get("html_url", ""))
    return ""


def dismiss_previous_reviews(token: str, context: PRContext, bot_login: str = "github-actions[bot]") -> int:
    """
    Dismisses previous Bruriah architectural reviews on the PR.

    Args:
        token: GitHub token.
        context: PRContext.
        bot_login: The login name of the bot user to look for.

    Returns:
        Number of dismissed reviews.
    """
    url = f"https://api.github.com/repos/{context.owner}/{context.repo}/pulls/{context.pr_number}/reviews"

    try:
        reviews = _github_api("GET", url, token)
    except GitHubError:
        return 0

    if not isinstance(reviews, list):
        return 0

    dismissed_count = 0
    for review in reviews:
        if not isinstance(review, dict):
            continue

        user = review.get("user", {})
        login = user.get("login") if isinstance(user, dict) else None

        body = review.get("body", "")
        state = review.get("state", "")

        if login == bot_login and "Bruriah Architectural Review" in body and state != "DISMISSED":
            review_id = review.get("id")
            if not review_id:
                continue

            dismiss_url = f"{url}/{review_id}/dismissals"
            dismiss_payload = {"message": "Dismissing previous architectural review.", "event": "DISMISS"}
            try:
                _github_api("PUT", dismiss_url, token, dismiss_payload)
                dismissed_count += 1
            except GitHubError:
                pass

    return dismissed_count
