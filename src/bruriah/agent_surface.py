# Agent-surface enforcement (bruriah --agent):
# The single place where the `--agent` rendering boundary is enforced in code rather than
# asserted in a comment. Every renderer that builds a string for a coding agent routes its
# non-literal values through this module.
"""Enforcement point for the `--agent` rendering boundary.

The invariant: every string in an `--agent` rendering is one of exactly three things --

1. a literal authored in this repository,
2. a value from a closed vocabulary, or
3. a format-validated identifier (a commit sha, a repository-relative path).

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

Every function is total: it returns a safe literal rather than raising, because a renderer has
no useful recovery from a malformed identifier and must never fall back to interpolating the
raw value. `UNKNOWN` and `<unprintable path>` are themselves literals authored here, so a
rejected value still satisfies case 1.

A leaf on purpose: it imports nothing from `bruriah`, so any module can route through it
without creating a cycle, and `tests/test_architecture.py` stays satisfied.
"""

from __future__ import annotations

import re
import unicodedata

# The decision statuses this repository recognises. `_metadata` in `corpus.py` does
# `frontmatter.get("status") or "unknown"` with no validation against a closed set, so the
# value arrives from a document and is mapped here rather than quoted.
KNOWN_DECISION_STATUSES: frozenset[str] = frozenset({"active", "superseded", "deprecated", "amended"})

# The lineage relations this repository recognises. `index.py` writes exactly these three, but
# that is enforced here rather than trusted from there.
KNOWN_LINEAGE_STATES: frozenset[str] = frozenset({"supersedes", "deprecates", "amends"})

# What a rejected value renders as. Both are literals authored in this repository.
UNKNOWN = "UNKNOWN"
UNPRINTABLE_PATH = "<unprintable path>"

# Git object names: short (7) through full SHA-256 (64). Matched with `fullmatch` so a trailing
# newline cannot slip past `$`.
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

# Long enough for any real repository-relative path, short enough that a pathological value
# cannot flood an agent's context window.
_MAX_PATH_LENGTH = 256

# A backtick ends the markdown code span these paths are rendered inside, so a path containing
# one breaks out of the span and its remainder stops reading as a quoted identifier.
_FORBIDDEN_PATH_CHARS = frozenset("`")


def closed(value: str, allowed: frozenset[str]) -> str:
    """Render `value` upper-cased if it belongs to `allowed`, else `UNKNOWN`.

    Comparison is on the stripped, lower-cased form, so the caller may pass whatever casing the
    producing module happens to use.
    """
    normalised = value.strip().lower()
    return normalised.upper() if normalised in allowed else UNKNOWN


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


def repo_path(value: str) -> str:
    """Render `value` as a repository-relative path, or `UNPRINTABLE_PATH` if it is implausible.

    Rejects the empty string, anything over `_MAX_PATH_LENGTH`, and anything carrying a control
    character -- which covers newline and carriage return (either would forge a new line in the
    rendering), the ANSI escape (which would forge terminal styling), and DEL -- plus the
    backtick, which would break out of the markdown code span.
    """
    if not value or len(value) > _MAX_PATH_LENGTH:
        return UNPRINTABLE_PATH
    for char in value:
        if char in _FORBIDDEN_PATH_CHARS or unicodedata.category(char) == "Cc":
            return UNPRINTABLE_PATH
    return value
