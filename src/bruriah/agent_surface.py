# Agent-surface enforcement (bruriah --agent):
# The single place where the `--agent` rendering boundary is enforced in code rather than
# asserted in a comment. Every renderer that builds a string for a coding agent routes its
# non-literal values through this module.
"""Enforcement point for the `--agent` rendering boundary.

The invariant: every string in an `--agent` rendering is one of exactly three things --

1. a literal authored in this repository,
2. a value from a closed vocabulary, or
3. an identifier whose FORMAT this module validates.

Repository-authored free text is named by reference, never quoted into a block that reads as
instruction to whatever consumes it. An agent that wants that text runs `bruriah why` or
`git show`; it is withheld, not unreachable.

This module is where cases 2 and 3 are ENFORCED. Before it existed, each renderer asserted the
closure in a comment pointing at whichever module was believed to write the value --
`index.py` for the lineage relation, `_metadata` in `corpus.py` for the decision status -- and
`brief.py` was the only one of the three that actually mapped anything through a known set. A
comment naming another module is not enforcement: it rots when that module changes, it cannot
be tested, and it gave three renderers three different behaviours for the same stated rule.
Here the rule is a function, it is called at every site, and it is covered by
`tests/test_agent_surface.py`.

**What case 3 does and does not prove.** `commit_sha` proves what its name says: a value that
is not 7 to 64 hex characters is not rendered as an identifier. `printable_path` proves much
less, and its name is deliberately narrow: it proves only that a value cannot forge a line,
forge terminal styling, or close the markdown code span it is rendered inside. It does NOT
validate path shape, absoluteness or containment -- `/etc/passwd`, `../../outside` and an
ordinary English sentence all pass, on purpose, because the renderers print revision ranges,
`path:line` targets and whatever target the operator typed through the same function. It was
called `repo_path` and documented as rendering "a repository-relative path", which overstated
it in the one direction that matters for a security boundary. Read it as "safe to print".

Every function is total: it returns a safe literal rather than raising, because a renderer has
no useful recovery from a malformed identifier and must never fall back to interpolating the
raw value. `None` is accepted everywhere for the same reason -- these fields are typed `str`
by convention only, several are built from document frontmatter, and an `AttributeError` here
aborts the whole command with a traceback. `UNKNOWN`, `<unprintable path>` and
`UNRECOGNISED_ACTION` are themselves literals authored here, so a rejected value still
satisfies case 1.

A rejection is never silent. `report_degradation` appends `DEGRADED_NOTICE` to the rendering
and writes one line to stderr, because the raw value survives in the human and JSON
renderings and an operator who cannot see that a substitution happened cannot act on it.

A leaf on purpose: it imports nothing from `bruriah`, so any module can route through it
without creating a cycle, and `tests/test_architecture.py` stays satisfied.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import TextIO

# The decision statuses this repository recognises. `_metadata` in `corpus.py` does
# `frontmatter.get("status") or "unknown"` with no validation against a closed set, so the
# value arrives from a document and is mapped here rather than quoted.
KNOWN_DECISION_STATUSES: frozenset[str] = frozenset({"active", "superseded", "deprecated", "amended"})

# The lineage relations this repository recognises. `index.py` writes exactly these three, but
# that is enforced here rather than trusted from there.
KNOWN_LINEAGE_STATES: frozenset[str] = frozenset({"supersedes", "deprecates", "amends"})

# The guard severities. `evaluate_guard` writes only these two, but `GuardViolation.severity`
# is an untyped `str` and the agent rendering interpolated it raw while its docstring called
# the vocabulary closed.
KNOWN_SEVERITIES: frozenset[str] = frozenset({"veto", "warning"})

# The impact risk levels, as `analyze_impact` reports them and `evaluate_brief` orders them.
KNOWN_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "critical"})

# The remediation actions `_synthesize_steps` in `heal.py` builds, in the exact form it authors
# them. `RemediationStep.action` is an untyped free string rendered in bold inside a numbered
# recipe -- the most instruction-shaped position in that whole rendering -- so membership is
# checked rather than trusted. `tests/test_heal.py` asserts `_synthesize_steps` and this set
# cannot drift apart.
KNOWN_REMEDIATION_ACTIONS: frozenset[str] = frozenset(
    {
        "Isolate Non-Compliant Code",
        "Apply Canonical Architectural Pattern",
        "Verify Architectural Compliance",
    }
)

# What a rejected value renders as. All three are literals authored in this repository.
UNKNOWN = "UNKNOWN"
UNPRINTABLE_PATH = "<unprintable path>"
UNRECOGNISED_ACTION = "Unrecognised remediation step"

# Said in the rendering itself, so an agent reading it knows the placeholder is not an
# identifier and knows where the value it replaced can still be read.
DEGRADED_NOTICE = (
    "**Note**: one or more values above could not be validated as an identifier or as a known "
    "value, and are shown as placeholders. Where that happened, the decision could not be "
    "identified safely and no command to read it is printed. Run this command without "
    "`--agent` to see the raw values."
)

# Git object names: short (7) through full SHA-256 (64). Matched with `fullmatch` so a trailing
# newline cannot slip past `$`.
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

# The length the renderings abbreviate a sha to.
SHORT_SHA_LENGTH = 8

# Long enough for any real repository-relative path, short enough that a pathological value
# cannot flood an agent's context window.
_MAX_PATH_LENGTH = 256

# A backtick ends the markdown code span these paths are rendered inside, so a path containing
# one breaks out of the span and its remainder stops reading as a quoted identifier.
_FORBIDDEN_CHARS = frozenset("`")

# Unicode categories that can forge structure the operator never wrote:
#   Cc  control characters -- newline and carriage return forge a new line, the ANSI escape
#       forges terminal styling, NUL and DEL truncate unpredictably.
#   Zl  U+2028 LINE SEPARATOR, and
#   Zp  U+2029 PARAGRAPH SEPARATOR -- line breaks to a great many renderers, so they forge a
#       new line exactly as `\n` does.
#   Cf  format characters -- a bidi override such as U+202E reorders everything after it, so
#       the text a reader sees is not the text that is there, and a zero-width joiner is
#       simply invisible.
# Only `Cc` was rejected before, which let the other three classes through, every one of them
# invisible in a diff.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# Every placeholder this module can substitute. `report_degradation` looks for these in a
# finished rendering: they are the only record a rejection happened, and scanning once per
# rendering is what keeps the warning to one line however many values were rejected.
_PLACEHOLDERS = (UNKNOWN, UNPRINTABLE_PATH, UNRECOGNISED_ACTION)


def closed(value: str | None, allowed: frozenset[str]) -> str:
    """Render `value` upper-cased if it belongs to `allowed`, else `UNKNOWN`.

    Comparison is on the stripped, lower-cased form, so the caller may pass whatever casing the
    producing module happens to use. `None` renders as `UNKNOWN` rather than raising: this
    function is called on fields built from document frontmatter and from other modules'
    dataclasses, and a traceback out of a renderer takes the whole command down.
    """
    if value is None:
        return UNKNOWN
    normalised = value.strip().lower()
    return normalised.upper() if normalised in allowed else UNKNOWN


def authored(value: str | None, allowed: frozenset[str], fallback: str) -> str:
    """Render `value` in its authored form if it belongs to `allowed`, else `fallback`.

    The counterpart to `closed` for multi-word labels this repository authors, such as the
    remediation actions: `closed` upper-cases, which suits a single-token badge and disfigures
    "Isolate Non-Compliant Code". Membership is exact apart from surrounding whitespace --
    the vocabulary is a set of literals from this repository, so a near-miss in casing is a
    value some other module wrote and is treated as one.
    """
    if value is None:
        return fallback
    normalised = value.strip()
    return normalised if normalised in allowed else fallback


def commit_sha(value: str | None) -> str:
    """Render `value` as a lower-cased commit sha, or `UNKNOWN` if it is not one.

    `None` and the empty string are not shas and render as `UNKNOWN`: the renderers hold
    optional successor shas, and a blank there must not become a blank identifier an agent
    could read as "no constraint".
    """
    if value is None:
        return UNKNOWN
    if _COMMIT_SHA.fullmatch(value) is None:
        return UNKNOWN
    return value.lower()


def short_commit_sha(value: str | None) -> str:
    """Render `value` as an abbreviated commit sha, or `UNKNOWN` if it is not one.

    Validation happens on the WHOLE value, before truncation, and the order is the point.
    Truncating first -- which is what `brief` did as `commit_sha(c.commit_sha[:8])`, and what
    `SupersedeTemplate.to_markdown` did with no validation at all -- checks only the prefix, so
    a malformed value whose first eight characters happen to be hex passes as an identifier
    while the discarded remainder is exactly where a backtick or a newline would sit. Here a
    value is either a sha in full or it is not one at all.

    The placeholder is returned whole rather than sliced, so a rejection never renders as a
    fragment of one.
    """
    rendered = commit_sha(value)
    return rendered if rendered == UNKNOWN else rendered[:SHORT_SHA_LENGTH]


def printable_path(value: str | None) -> str:
    """Render `value` if it is safe to print, or `UNPRINTABLE_PATH` if it is not.

    Safe to print, and nothing more. This rejects the empty value and `None`, anything over
    `_MAX_PATH_LENGTH`, the backtick that would break out of the markdown code span these
    values are rendered inside, and anything in the Unicode categories listed at
    `_FORBIDDEN_CATEGORIES` -- the characters that can forge a line break, forge terminal
    styling or reorder what a reader sees.

    It does NOT validate that the value is a path, that it is relative, or that it is inside
    the repository: an absolute path, a `..` escape and an ordinary English sentence all pass.
    That is deliberate. The renderers print git-derived paths, `path:line` targets, revision
    ranges and whatever target the operator typed through this one function, so a containment
    check here would reject values that are correct. If containment is ever wanted it needs its
    own function and its own call sites; do not read this one as providing it.
    """
    if not value or len(value) > _MAX_PATH_LENGTH:
        return UNPRINTABLE_PATH
    for char in value:
        if char in _FORBIDDEN_CHARS or unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            return UNPRINTABLE_PATH
    return value


def report_degradation(rendering: str, *, command: str, stream: TextIO | None = None) -> str:
    """Announce that `rendering` carries a placeholder, in the rendering and on stderr.

    A rejection used to be silent. The rendering carried `UNKNOWN` or `<unprintable path>`,
    the human and JSON renderings still carried the raw value, and nothing told the operator
    that the two now disagreed -- so a poisoned identifier looked exactly like a decision
    whose sha happened to be missing.

    The check runs once on the finished rendering rather than at each call above, for two
    reasons. The placeholders ARE the record of a rejection, since every function here is
    total and returns one instead of raising. And the warning has to be one line per
    rendering: a guard run with twenty violations must not print the same fact twenty times.

    The cost of reading the record rather than counting the rejections is a false positive if a
    literal in the rendering, or an operator's own task intent, happens to contain one of the
    placeholder strings. That is a spurious warning about text nobody rejected, which is the
    harmless direction for this to fail in.
    """
    if not any(placeholder in rendering for placeholder in _PLACEHOLDERS):
        return rendering
    print(
        f"bruriah {command}: one or more values in the --agent rendering could not be validated "
        f"and were replaced with a placeholder; run without --agent to see the raw values.",
        file=stream if stream is not None else sys.stderr,
    )
    return f"{rendering}\n\n{DEGRADED_NOTICE}"
