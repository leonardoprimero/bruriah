"""Find question/answer pairs whose QUESTION came from someone who did not know the answer.

WHY. `evals/project-memory/` is twelve questions written by the person who wrote the commits that
answer them, and `leakage.py` measures what that costs: ten of those twelve hand over a term the
corpus treats as distinctive. Writing more questions the same way makes the sample bigger and the
confound no smaller. The fix is not more discipline while writing questions -- it is a question
source that is independent of the answer BY CONSTRUCTION.

Git histories already contain one. A commit whose body says `closes #123` names the request it
answers, and if issue #123 was opened by somebody else, before the commit existed, then the
question was phrased by a person who did not yet know what the answer would be. Nothing about that
depends on the eval author's self-restraint, and every clause of it is checkable after the fact by
a stranger.

THE FOUR CONDITIONS, three of them mechanical:

  1. The commit body names an issue it closes. `parse_closes`.
  2. The reference is an ISSUE, not a pull request. A PR is the answer wearing a number.
  3. The issue's author is not the commit's author, and the issue predates the commit.
     `independence`. The author comparison is what does the work: over `square/leakcanary`'s whole
     history it rejected 123 of 307 references, the maintainer filing an issue about their own
     fix. The timestamp clause rejected exactly 1 more, and is kept anyway because it costs a
     comparison and closes a hole the author test cannot see -- a maintainer who writes the fix,
     then files the issue, under a commit that carries a co-author's handle. Cheap insurance, not
     a major filter, and worth saying so rather than letting a reader assume both clauses pull
     equal weight.
  4. The issue title works as a question. NOT mechanical, and deliberately left to a human:
     `ToastEventListener leak` is usable and `Bugs when running application` is not, and no rule
     written here would separate them without also deciding what a good question is, which is the
     judgement this whole module exists to keep out of the mechanical part.

Conditions 1-3 are decided here and every rejection is recorded with its reason, so the set that
survives can be audited against the set that did not. A pipeline that reports only its keepers is
asking to be trusted about the ones it dropped.

Ground truth is matched to the corpus BY SHA rather than by rebuilding the filename. `bruriah
corpus` writes `{date}-{sha8}-{slug}.md`, and reimplementing that slug here would be a second
copy free to drift from the shipped one; the sha is already in the name and is exact. The recorded
ground truth then carries no sha, per the convention `evals/project-memory/README.md` documents:
a sha does not survive a history rewrite and the eval is meant to outlive one.

No I/O in this module. `extract_pairs.py` does the git and GitHub work and calls in here.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

# `closes #12`, `Fixes: #12`, `resolved #12`. The bounded gap tolerates `closes issue #12` without
# letting a closing keyword bind to a number a paragraph away.
_CLOSES = re.compile(
    r"\b(?:clos(?:e|es|ed)|fix(?:e[sd])?|resolv(?:e|es|ed))\b[^\n#]{0,20}#(\d+)", re.IGNORECASE
)
_SHA_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-([0-9a-f]{8})-(.*)$")


def parse_closes(body: str) -> list[int]:
    """Issue numbers this commit body claims to close, in order, deduplicated.

    Only bodies. A subject line records what changed, never why, and `bruriah corpus` already
    refuses to index a commit that has nothing else -- taking a closing reference from a subject
    would pair a question with a document the corpus does not contain.
    """
    seen: dict[int, None] = {}
    for match in _CLOSES.finditer(body or ""):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_login: str | None
    authored_at: datetime
    subject: str
    body: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    author_login: str
    created_at: datetime
    is_pull_request: bool


def independence(commit: Commit, issue: Issue) -> str | None:
    """`None` when the pair is independent; otherwise the reason it is not.

    Returning the reason rather than a bool is what lets the extractor publish its rejects with
    the ground for each one. A filter that can only say no teaches nobody anything.

    Author comparison prefers GitHub logins, which are exact. It falls back to comparing the git
    author's display name against the issue author's login only when no login is known, and that
    fallback is deliberately GENEROUS about declaring sameness: a false "same author" discards a
    usable pair, while a false "independent" would smuggle a self-authored question into the set
    this exists to keep clean. Discarding is the cheap error.
    """
    if issue.is_pull_request:
        return "reference is a pull request, not an issue"

    if commit.author_login and issue.author_login:
        if commit.author_login.casefold() == issue.author_login.casefold():
            return "issue author is the commit author"
    else:
        name = commit.author_name.casefold()
        login = issue.author_login.casefold()
        parts = [part for part in re.split(r"[^a-z0-9]+", name) if len(part) > 2]
        if login in name.replace(" ", "") or any(part and part in login for part in parts):
            return "issue author appears to be the commit author (name match, no login available)"

    if issue.created_at >= commit.authored_at:
        # The maintainer-writes-up-their-own-fix shape. It clears the author test whenever a
        # second person's handle is on the commit, so the timestamp is what actually catches it.
        return "issue was opened at or after the commit, so it cannot have prompted it"
    return None


@dataclass(frozen=True)
class Pair:
    """One surviving question/answer pair, ready to become an eval case."""

    issue_number: int
    question: str
    ground_truth: str
    commit_sha: str
    issue_author: str
    commit_author: str


@dataclass(frozen=True)
class Rejected:
    commit_sha: str
    issue_number: int | None
    reason: str


def corpus_index(filenames: Iterable[str]) -> dict[str, str]:
    """Map sha8 -> the corpus filename with its sha REMOVED, which is how ground truth is recorded.

    Built from the filenames `bruriah corpus` actually produced, so a commit the corpus skipped --
    one whose body was empty, though such a commit could not have carried a closing reference
    anyway -- simply has no entry and is reported as unmatched rather than silently paired with
    a document that does not exist.
    """
    index: dict[str, str] = {}
    for name in filenames:
        match = _SHA_IN_NAME.match(name)
        if match:
            index[match.group(2)] = f"{match.group(1)}-{match.group(3)}"
    return index


def build(
    commits: Sequence[Commit],
    issues: Mapping[int, Issue],
    corpus: Mapping[str, str],
) -> tuple[list[Pair], list[Rejected]]:
    """Apply conditions 1-3 to every commit, returning what survived and what did not, with reasons.

    `issues` maps number -> Issue for every reference that could be resolved; a number absent from
    it is rejected as unresolvable rather than assumed innocent.
    """
    kept: list[Pair] = []
    rejected: list[Rejected] = []
    for commit in commits:
        numbers = parse_closes(commit.body)
        if not numbers:
            continue  # not a candidate at all; not a rejection worth reporting
        document = corpus.get(commit.sha[:8])
        if document is None:
            rejected.append(Rejected(commit.sha[:12], None, "commit is not in the derived corpus"))
            continue
        for number in numbers:
            issue = issues.get(number)
            if issue is None:
                rejected.append(Rejected(commit.sha[:12], number, "issue could not be resolved"))
                continue
            reason = independence(commit, issue)
            if reason is not None:
                rejected.append(Rejected(commit.sha[:12], number, reason))
                continue
            kept.append(Pair(
                issue_number=number,
                question=issue.title,
                ground_truth=document,
                commit_sha=commit.sha[:12],
                issue_author=issue.author_login,
                commit_author=commit.author_login or commit.author_name,
            ))
    return kept, rejected


__all__ = [
    "Commit", "Issue", "Pair", "Rejected", "build", "corpus_index", "independence", "parse_closes",
]
