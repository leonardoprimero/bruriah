"""Turn the GitHub issues and pull requests a commit links to into corpus documents.

This is `bruriah corpus --github` (T3): the CLI hands this module the same non-merge commits
`gitcorpus.build` already walks (`gitcorpus.walk_commits`), and for every issue/PR one of them
links, it writes one Markdown document a reader recognizes the same way any other corpus document
is recognized -- front matter `corpus._metadata` already understands, a title, a body, and a
provenance trail back to GitHub.

Design decisions, carried over verbatim from `odd/tasks/github-issue-pr-ingestion.md` (T3 scope):

- **Only `closes` links are followed.** `issue_links.linked_issues` also reports `mentions`, which
  is deliberately ignored here: a bare `#N` mention is exactly as likely to be small talk ("see
  #12 for context") as it is to be the issue a commit actually closes, and treating every mention
  as a document-worthy link would flood the corpus with tangential issues for every one that
  actually explains a decision. `pull_request_self` (the squash-merge `(#N)` subject suffix) is
  followed differently: it names the PR the commit CAME FROM, so this module fetches that PR and
  re-runs `linked_issues` over ITS title and body, because a squash merge on GitHub carries the
  original branch's PR body -- and the `Closes #N` line a contributor wrote on the PR -- into a
  single commit whose own message is often just the PR title.
- **Rejected alternatives come from GitHub state, not from a template.** A cross-referenced pull
  request that was closed without being merged, or a cross-referenced issue closed with
  `state_reason: not_planned`, is treated as a rejected alternative to the issue that references
  it -- but only when that cross-reference's source lives in the corpus repo itself. A real GitHub
  timeline can carry a `cross-referenced` event whose source is a fork or a downstream project;
  that source's number means nothing in the corpus repo, so it is counted alongside cross-repo
  commit links and never fetched, exactly like the `closes`/`mentions` cross-repo case above. The
  rejection reason is the last comment posted before the item closed, trimmed to
  `_REASON_MAX_CHARS`; an item closed with no comment at all gets the fixed phrase
  `_NO_COMMENT_REASON` instead of an empty string, so the document never claims a reason it does
  not have.
- **Premises are read only from an explicit `Premise:` line** in the issue/PR body, using the same
  `id | statement` grammar `gitcorpus` uses for the commit-trailer form (see
  `gitcorpus._split_premise_value`) -- no NLP extraction, so a premise only ever enters the corpus
  because someone wrote one down.
- **Failure is per-issue, never per-build.** `GitHubNotFoundError` and every other `GitHubError`
  (including `GitHubOfflineError`, its offline-cache-miss subclass) are caught per resolved issue
  number: that one issue is skipped, a warning goes to stderr, and every other issue still gets
  built. A commit history with one broken link should not cost a reader the rest of their corpus.
- **Provenance without a contract change.** `EvidenceRecord.publisher` (`contracts.py`) is set at
  query time from a passage's `relative_path` (see `retrieval.py`/`service.py`), which this module
  cannot touch (T3 scope explicitly excludes `contracts.py`, `service.py`, and `mcp_server.py`).
  The filename convention below (`YYYY-MM-DD-issue-N-<slug>.md`) makes that same `relative_path`
  -- and therefore `publisher` -- read as GitHub-derived without any code downstream ever being
  told what a GitHub document is. `source_url` in the front matter is `corpus._metadata`'s own
  existing `url`-family key (already read into `SourceMetadata.provenance_urls`), so a reader who
  parses the document programmatically gets a second, structured pointer back to GitHub, entirely
  through keys `_metadata` already understood before this module existed.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .github import GitHubError
from .github_read import (
    ResponseCache,
    Transport,
    _default_transport,
    get_issue,
    get_issue_comments,
    get_issue_timeline,
    get_pull,
)
from .gitcorpus import WalkedCommit, _slug, _split_premise_value
from .issue_links import linked_issues

_REASON_MAX_CHARS = 300
_NO_COMMENT_REASON = "closed without merge; no closing comment"
_MANIFEST_NAME = "github-manifest.json"


class GitHubCorpusError(ValueError):
    """Raised for a `bruriah corpus --github` configuration problem detected before any GitHub
    call is made -- an unparseable `--github OWNER/REPO`, or `--github` (bare) over a repository
    whose `origin` remote is missing or is not a GitHub URL this module recognizes."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class GitHubCorpusResult:
    documents_written: int
    commits_scanned: int
    issues_fetched: int
    issues_skipped: int
    cross_repo_skipped: int
    cache_hits: int
    network_calls: int
    manifest_path: Path


def _run_git(repo: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


_REMOTE_PATTERNS = (
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
)


def detect_repo_slug(repo: Path) -> str:
    """Derive `owner/repo` from the `origin` remote, for a bare `--github` with no explicit slug.

    Both remote forms GitHub itself offers on a repository's own page are recognized: the `https://`
    clone URL and the `git@github.com:` SCP-like SSH form, plus the less common `ssh://` form -- all
    three name the same two things (owner, repo) in a different shape.
    """
    import re
    import subprocess

    try:
        url = _run_git(repo, "config", "--get", "remote.origin.url")
    except FileNotFoundError as error:
        raise GitHubCorpusError("git_not_on_path") from error
    except subprocess.CalledProcessError as error:
        raise GitHubCorpusError(
            "github_repo_autodetect_failed",
            "no 'origin' remote is configured -- pass --github OWNER/REPO explicitly.",
        ) from error
    if not url:
        raise GitHubCorpusError(
            "github_repo_autodetect_failed",
            "the 'origin' remote has no URL -- pass --github OWNER/REPO explicitly.",
        )
    for pattern in _REMOTE_PATTERNS:
        match = re.match(pattern, url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    raise GitHubCorpusError(
        "github_repo_autodetect_unrecognized",
        f"'origin' remote {url!r} is not a github.com URL -- pass --github OWNER/REPO explicitly.",
    )


def _extract_premises(body: str) -> list[dict[str, Any]]:
    """Explicit `Premise:` lines only -- the same one-line grammar `gitcorpus` reads from a commit
    trailer, applied here to free-form issue/PR body text instead. No other line is inspected."""
    premises: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped.lower().startswith("premise:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if not value:
            continue
        premise_id, statement = _split_premise_value(value)
        premises.append({"id": premise_id, "statement": statement, "status": "active"})
    return premises


def _closing_reason(comments: list[dict[str, Any]], closed_at: str | None) -> str:
    """The last comment posted before `closed_at`, trimmed to `_REASON_MAX_CHARS`; the fixed
    fallback phrase when nothing qualifies. ISO 8601 UTC timestamps (`...Z`) sort correctly as
    plain strings, so no datetime parsing is needed to find "before"."""
    candidates = [
        comment
        for comment in comments
        if isinstance(comment, dict)
        and isinstance(comment.get("body"), str)
        and comment["body"].strip()
        and (closed_at is None or str(comment.get("created_at") or "") <= closed_at)
    ]
    if not candidates:
        return _NO_COMMENT_REASON
    candidates.sort(key=lambda comment: str(comment.get("created_at") or ""))
    return candidates[-1]["body"].strip()[:_REASON_MAX_CHARS]


@dataclass
class _Ledger:
    """Mutable bookkeeping threaded through one build, kept off `GitHubCorpusResult` (frozen,
    returned once at the end) so every counter has exactly one place it is incremented."""

    cache: ResponseCache
    owner: str
    repo: str
    token: str | None
    network_enabled: bool
    transport: Transport
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    cache_hits: int = 0
    network_calls: int = 0

    def _seen(self, path: str) -> None:
        if self.cache.get(path) is not None:
            self.cache_hits += 1
        else:
            self.network_calls += 1

    def issue(self, number: int) -> dict[str, Any]:
        path = f"/repos/{self.owner}/{self.repo}/issues/{number}"
        self._seen(path)
        return get_issue(
            self.owner, self.repo, number, cache=self.cache, token=self.token,
            network_enabled=self.network_enabled, transport=self.transport,
            clock=self.clock, sleep=self.sleep,
        )

    def pull(self, number: int) -> dict[str, Any]:
        path = f"/repos/{self.owner}/{self.repo}/pulls/{number}"
        self._seen(path)
        return get_pull(
            self.owner, self.repo, number, cache=self.cache, token=self.token,
            network_enabled=self.network_enabled, transport=self.transport,
            clock=self.clock, sleep=self.sleep,
        )

    def timeline(self, number: int) -> list[Any]:
        path = f"/repos/{self.owner}/{self.repo}/issues/{number}/timeline"
        self._seen(path)
        return get_issue_timeline(
            self.owner, self.repo, number, cache=self.cache, token=self.token,
            network_enabled=self.network_enabled, transport=self.transport,
            clock=self.clock, sleep=self.sleep,
        )

    def comments(self, number: int) -> list[Any]:
        path = f"/repos/{self.owner}/{self.repo}/issues/{number}/comments"
        self._seen(path)
        return get_issue_comments(
            self.owner, self.repo, number, cache=self.cache, token=self.token,
            network_enabled=self.network_enabled, transport=self.transport,
            clock=self.clock, sleep=self.sleep,
        )


def _resolve_links_for_commit(
    commit: WalkedCommit, ledger: _Ledger,
) -> tuple[set[int], int]:
    """The same-repo issue numbers `commit` closes, plus how many cross-repo links it named (never
    fetched -- see the module docstring). `mentions` links are dropped here, permanently."""
    links = linked_issues(commit.subject, commit.body)
    numbers: set[int] = set()
    cross_repo = 0
    for link in links:
        if link.kind == "closes":
            if link.repo is not None:
                cross_repo += 1
            else:
                numbers.add(link.number)
        elif link.kind == "pull_request_self":
            try:
                pr = ledger.pull(link.number)
            except GitHubError:
                continue
            for inner in linked_issues(str(pr.get("title", "")), str(pr.get("body") or "")):
                if inner.kind != "closes":
                    continue
                if inner.repo is not None:
                    cross_repo += 1
                else:
                    numbers.add(inner.number)
    return numbers, cross_repo


def _rejected_alternative_from_pull(pull: dict[str, Any], ledger: _Ledger, number: int) -> dict[str, Any] | None:
    if pull.get("state") != "closed" or pull.get("merged_at") is not None:
        return None
    comments = ledger.comments(number)
    return {
        "name": str(pull.get("title", "")),
        "disposition": "rejected",
        "reason": _closing_reason(comments, pull.get("closed_at")),
    }


def _rejected_alternative_from_issue(issue: dict[str, Any], ledger: _Ledger, number: int) -> dict[str, Any] | None:
    if issue.get("state_reason") != "not_planned":
        return None
    comments = ledger.comments(number)
    return {
        "name": str(issue.get("title", "")),
        "disposition": "rejected",
        "reason": _closing_reason(comments, issue.get("closed_at")),
    }


_HTML_URL_REPO = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:issues|pull)/\d+/?$"
)


def _source_repo_slug(src_issue: dict[str, Any]) -> str | None:
    """The `owner/repo` a timeline `cross-referenced` event's source issue/PR belongs to, or
    `None` when it cannot be determined. `repository.full_name` is the field GitHub's own
    timeline API puts on a cross-reference source; `html_url` is the fallback for a payload (or a
    fixture) that omits it, parsed with the same `/(issues|pull)/<n>` shape `issue_links` already
    recognizes for a same-repo-vs-cross-repo reference in commit text."""
    repository = src_issue.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name:
            return full_name
    html_url = src_issue.get("html_url")
    if isinstance(html_url, str):
        match = _HTML_URL_REPO.match(html_url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def _collect_alternatives(number: int, ledger: _Ledger, repo: str) -> tuple[list[dict[str, Any]], int]:
    """The rejected-alternative documents for `number`'s cross-referenced timeline events, plus how
    many of those events were skipped because their source lives in another repository -- a fork
    or a downstream project cross-referencing this issue, which GitHub's real timelines do carry.
    A foreign source is counted here exactly like a cross-repo `closes`/`mentions` commit link
    (`_resolve_links_for_commit`) and never fetched: its number means nothing in `repo`, and
    fetching it risks pulling an unrelated same-numbered PR/issue into the document, or a 404 that
    would otherwise drop the whole issue (see the module docstring)."""
    alternatives: list[dict[str, Any]] = []
    cross_repo_skipped = 0
    for event in ledger.timeline(number):
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = event.get("source") or {}
        src_issue = source.get("issue")
        if not isinstance(src_issue, dict):
            continue
        src_number = src_issue.get("number")
        if not isinstance(src_number, int):
            continue
        source_repo = _source_repo_slug(src_issue)
        if source_repo is None or source_repo.lower() != repo.lower():
            cross_repo_skipped += 1
            continue
        if "pull_request" in src_issue:
            pull = ledger.pull(src_number)
            alt = _rejected_alternative_from_pull(pull, ledger, src_number)
        else:
            full_issue = ledger.issue(src_number)
            alt = _rejected_alternative_from_issue(full_issue, ledger, src_number)
        if alt is not None:
            alternatives.append(alt)
    return alternatives, cross_repo_skipped


def _render_document(
    *,
    issue: dict[str, Any],
    number: int,
    repo: str,
    linking_commits: Sequence[WalkedCommit],
    alternatives: list[dict[str, Any]],
    premises: list[dict[str, Any]],
) -> str:
    title = str(issue.get("title", "")).strip() or f"Issue {number}"
    html_url = str(issue.get("html_url", ""))

    # Built as a dict and serialized with `yaml.safe_dump`, unlike `gitcorpus`'s hand-built
    # frontmatter lines: a commit trailer is short, single-line text the person committing wrote
    # for this exact purpose, but an issue/PR title or a review comment is free-form text nobody
    # wrote with YAML in mind -- "Attempt: retry on lock contention" is a completely ordinary PR
    # title that breaks an unquoted `name: <value>` line the moment it reaches a `:` of its own.
    frontmatter_data: dict[str, Any] = {
        "commit": linking_commits[0].sha,
        "issue": number,
        "github_url": html_url,
        "source_url": html_url,
    }
    all_premise_ids = [p["id"] for p in premises]
    if alternatives:
        frontmatter_data["alternatives"] = [
            {
                "name": alt["name"],
                "disposition": "rejected",
                **({"reason": alt["reason"]} if alt["reason"] else {}),
                **({"premises": list(all_premise_ids)} if all_premise_ids else {}),
            }
            for alt in alternatives
        ]
    if premises:
        frontmatter_data["premises"] = [
            {"id": premise["id"], "statement": premise["statement"], "status": "active"}
            for premise in premises
        ]
    frontmatter = "---\n" + yaml.safe_dump(
        frontmatter_data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    ) + "---\n\n"

    body_text = str(issue.get("body") or "").strip()
    alt_section = ""
    if alternatives:
        alt_section = "\n## Rejected alternatives\n\n" + "".join(
            f"- **{alt['name']}** (rejected): {alt['reason']}\n" for alt in alternatives
        )
    linked_section = "\n## Linked decisions\n\n" + "".join(
        f"- `{commit.sha[:8]}` {commit.subject}\n" for commit in linking_commits
    )
    return (
        frontmatter
        + f"# {title}\n\n"
        f"**Issue:** [#{number}]({html_url}) · **Repository:** {repo}\n\n"
        f"{body_text}\n"
        f"{alt_section}"
        f"{linked_section}"
    )


def build_documents(
    commits: Sequence[WalkedCommit],
    out: Path,
    *,
    repo: str,
    cache: ResponseCache,
    revision: str = "HEAD",
    token: str | None = None,
    network_enabled: bool = True,
    transport: Transport = _default_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> GitHubCorpusResult:
    """Write one document per issue/PR the given commits close, plus `github-manifest.json`.

    `commits` is exactly what `gitcorpus.walk_commits` returns for the same repository and
    revision -- every non-merge commit, bodiless ones included, so a squash-merge subject's `(#N)`
    suffix is seen even when the commit carries no body of its own.
    """
    if "/" not in repo:
        raise GitHubCorpusError("invalid_repo_slug", repo)
    owner, repo_name = repo.split("/", 1)
    ledger = _Ledger(
        cache=cache, owner=owner, repo=repo_name, token=token, network_enabled=network_enabled,
        transport=transport, clock=clock, sleep=sleep,
    )

    # issue number -> linking commits, in first-appearance order (dedup by sha per issue).
    linking_commits: dict[int, list[WalkedCommit]] = {}
    cross_repo_skipped = 0
    for commit in commits:
        numbers, cross_repo = _resolve_links_for_commit(commit, ledger)
        cross_repo_skipped += cross_repo
        for number in numbers:
            bucket = linking_commits.setdefault(number, [])
            if commit not in bucket:
                bucket.append(commit)

    out.mkdir(parents=True, exist_ok=True)
    documents: dict[str, str] = {}
    issues_fetched = 0
    issues_skipped = 0
    for number in sorted(linking_commits):
        try:
            issue = ledger.issue(number)
            alternatives, timeline_cross_repo = _collect_alternatives(number, ledger, repo)
            cross_repo_skipped += timeline_cross_repo
        except GitHubError as error:
            issues_skipped += 1
            print(
                f"warning: skipping issue #{number} ({repo}): {error}", file=sys.stderr,
            )
            continue
        issues_fetched += 1
        premises = _extract_premises(str(issue.get("body") or ""))
        document = _render_document(
            issue=issue, number=number, repo=repo, linking_commits=linking_commits[number],
            alternatives=alternatives, premises=premises,
        )
        date10 = str(issue.get("closed_at") or issue.get("created_at") or "")[:10] or "unknown-date"
        title = str(issue.get("title", ""))
        filename = f"{date10}-issue-{number}-{_slug(title)}.md"
        (out / filename).write_text(document, encoding="utf-8", newline="\n")
        documents[str(number)] = filename

    manifest_path = out / _MANIFEST_NAME
    manifest = {
        "repo": repo,
        "revision": revision,
        "counts": {
            "commits_scanned": len(commits),
            "links_found": sum(len(v) for v in linking_commits.values()),
            "issues_fetched": issues_fetched,
            "issues_skipped": issues_skipped,
            "cross_repo_skipped": cross_repo_skipped,
            "cache_hits": ledger.cache_hits,
            "network_calls": ledger.network_calls,
        },
        "documents": dict(sorted(documents.items(), key=lambda item: int(item[0]))),
    }
    # `sort_keys=False`: ordering is already deterministic by construction (insertion order for
    # the top-level fields, ascending issue number for `documents`) -- `sort_keys=True` would
    # re-sort `documents` alphabetically as strings ("100" before "41"), undoing that.
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    return GitHubCorpusResult(
        documents_written=len(documents),
        commits_scanned=len(commits),
        issues_fetched=issues_fetched,
        issues_skipped=issues_skipped,
        cross_repo_skipped=cross_repo_skipped,
        cache_hits=ledger.cache_hits,
        network_calls=ledger.network_calls,
        manifest_path=manifest_path,
    )


__all__ = [
    "GitHubCorpusError",
    "GitHubCorpusResult",
    "build_documents",
    "detect_repo_slug",
]
