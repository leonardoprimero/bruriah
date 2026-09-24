"""Parse GitHub issue/PR references out of a commit subject and body.

This is the grammar GitHub itself uses to link a commit or pull request to an issue
(https://docs.github.com/en/issues/tracking-your-work-with-issues/using-keywords-in-issues-and-pull-requests),
reimplemented as a pure function so `bruriah corpus` can decide, at build time and without any
network access, which issues a commit closes and which it merely mentions. The GitHub API would
give the same answer for an already-merged PR, but the corpus builder also has to read history
that was never pushed through GitHub's own linking (a squash-merge commit replayed onto a fresh
mirror, for instance), so the grammar has to be reimplemented rather than fetched.

Two exclusions matter enough to call out explicitly:

- Fenced code blocks and inline code spans are stripped before any reference is looked for.
  A commit body that quotes a diff or a shell session routinely contains `#5` as a shell
  comment marker or `close(fd)` in a snippet; matching those would manufacture links to issues
  the commit never mentions, and there is no way to tell a real reference from a quoted one
  without knowing whether the surrounding text is prose or code.

- The squash-merge subject suffix `(#N)` is its own `pull_request_self` kind, not a `mentions`
  hit, because it names something structurally different: GitHub appends it to the *squashed
  commit's own subject* to record which pull request the commit came from, not an issue the
  commit is about. Feeding that number back into the corpus as a linked issue would attach an
  issue's vocabulary to the PR that merged it -- confusing the very distinction (linked issue vs.
  origin PR) this module exists to preserve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Fenced code blocks first (they can themselves contain a lone backtick), then inline code
# spans. Both are replaced with a single space rather than deleted outright, so that text on
# either side of a removed span never fuses into something that looks like a reference by
# accident (e.g. "before`code`5" must not become "before5").
_FENCED_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_SPAN = re.compile(r"`[^`\n]*`")

_CLOSING_KEYWORD = re.compile(
    r"\b(?P<keyword>close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b(?::[ \t]*|[ \t]+)",
    re.IGNORECASE,
)

# A single reference, in any of the three forms GitHub recognizes. `[1-9]\d*` (rather than `\d+`)
# is what rejects a leading zero and rejects "0" outright -- GitHub does not treat #0 as a
# reference, and neither do we. The lookbehind/lookahead pair rejects a hash glued to a word
# character on either side, so "C#5" and "issue#5a" are skipped rather than misread.
_REFERENCE = re.compile(
    r"(?<![\w/])(?:"
    r"https?://github\.com/(?P<url_repo>[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)"
    r"/(?:issues|pull)/(?P<url_num>[1-9]\d*)"
    r"|(?P<repo>[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)#(?P<num>[1-9]\d*)"
    r"|#(?P<bare_num>[1-9]\d*)"
    r")(?!\w)"
)

_SQUASH_SUFFIX = re.compile(r"\(#([1-9]\d*)\)\s*$")


@dataclass(frozen=True)
class IssueLink:
    number: int
    kind: Literal["closes", "mentions", "pull_request_self"]
    repo: str | None  # "owner/repo" when the reference was cross-repo, else None
    keyword: str | None  # the matched keyword lowercased ("fixes", "closes", "resolves") or None


def _strip_code_spans(text: str) -> str:
    text = _FENCED_CODE_BLOCK.sub(" ", text)
    return _INLINE_CODE_SPAN.sub(" ", text)


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _reference_fields(match: re.Match[str]) -> tuple[int, str | None]:
    if match.group("url_num") is not None:
        return int(match.group("url_num")), match.group("url_repo")
    if match.group("num") is not None:
        return int(match.group("num")), match.group("repo")
    return int(match.group("bare_num")), None


def _scan_references(text: str) -> list[tuple[int, str | None, str | None]]:
    """Return (number, repo, keyword) for every reference in `text`, in order of first
    appearance. `keyword` is the lowercased closing keyword when the reference is immediately
    preceded by one (with a colon and/or whitespace separator), else `None`.

    A closing keyword is matched separately from a bare reference and then joined by span, rather
    than folded into one regex, because Python's `re` module forbids reusing a group name across
    alternation branches: the three reference forms already need three different number-group
    names (`url_num`, `num`, `bare_num`), and adding a keyword-bearing variant of each would
    triple that without changing what is actually being matched.
    """
    consumed: dict[tuple[int, int], str] = {}
    for keyword_match in _CLOSING_KEYWORD.finditer(text):
        reference_match = _REFERENCE.match(text, keyword_match.end())
        if reference_match is not None:
            consumed[(reference_match.start(), reference_match.end())] = keyword_match.group("keyword").lower()

    results: list[tuple[int, str | None, str | None]] = []
    for reference_match in _REFERENCE.finditer(text):
        number, repo = _reference_fields(reference_match)
        keyword = consumed.get((reference_match.start(), reference_match.end()))
        results.append((number, repo, keyword))
    return results


def _squash_suffix(subject: str) -> tuple[int, int, int] | None:
    """Return the (start, end, number) of a squash-merge suffix in `subject`, or `None`.

    Not supported: a subject where an explicit closing keyword sits directly against the
    trailing parenthesis (e.g. "Fixes (#123)") is still read as the squash suffix, not as an
    explicit closing reference. GitHub's automatic suffix always follows the PR title -- never a
    bare keyword -- so this shape is not expected from real squash merges, and distinguishing
    "deliberately authored" from "automatically appended" text with the same shape is not worth
    the complexity for a case the grammar reference does not call out.
    """
    match = _SQUASH_SUFFIX.search(subject)
    if match is None:
        return None
    return match.start(), match.end(), int(match.group(1))


def linked_issues(subject: str, body: str) -> tuple[IssueLink, ...]:
    """Extract every issue/PR reference GitHub's own linking grammar would recognize in a commit
    subject and body, ordered by first appearance (subject before body) and deduplicated by
    (number, repo). When the same reference appears both as `closes` and `mentions`, `closes`
    wins regardless of which occurrence came first.
    """
    subject_clean = _strip_code_spans(subject)
    body_clean = _strip_code_spans(body)

    result: list[IssueLink] = []
    index_by_key: dict[tuple[int, str | None], int] = {}

    def emit(number: int, repo: str | None, keyword: str | None) -> None:
        kind: Literal["closes", "mentions"] = "closes" if keyword is not None else "mentions"
        key = (number, repo)
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(result)
            result.append(IssueLink(number=number, kind=kind, repo=repo, keyword=keyword))
            return
        if kind == "closes" and result[existing_index].kind == "mentions":
            result[existing_index] = IssueLink(number=number, kind="closes", repo=repo, keyword=keyword)

    squash_span = _squash_suffix(subject_clean)
    if squash_span is not None:
        start, end, number = squash_span
        index_by_key[(number, None)] = len(result)
        result.append(IssueLink(number=number, kind="pull_request_self", repo=None, keyword=None))
        subject_clean = _mask(subject_clean, start, end)

    for number, repo, keyword in _scan_references(subject_clean):
        emit(number, repo, keyword)
    for number, repo, keyword in _scan_references(body_clean):
        emit(number, repo, keyword)

    return tuple(result)
