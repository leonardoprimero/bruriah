# PR review comment generation engine (Slice: PR Review Bot):
# Transforms a DriftReport (from drift.py) and optionally enriches it with
# line-level causal archaeology (from why.py) to produce structured review
# comments suitable for posting on pull requests via the GitHub API.
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .drift import DriftReport, DriftWarning

from .why import CausalResolution, WhyError, trace_causal_archaeology


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewComment:
    """A single inline review comment on a pull request.

    When ``line`` is ``None``, the comment applies to the whole file (GitHub
    renders it in the "Files changed" conversation tab).  When ``line`` is set,
    GitHub attaches the comment to that specific line in the diff.
    """

    path: str
    body: str
    line: int | None = None
    start_line: int | None = None
    side: str = "RIGHT"


@dataclass(frozen=True)
class ReviewResult:
    """A complete PR review with summary and inline comments."""

    body: str
    comments: tuple[ReviewComment, ...]
    event: str  # "COMMENT" or "REQUEST_CHANGES"
    has_drift: bool
    inspected_count: int
    stale_count: int
    clean_count: int
    unindexed_count: int


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(diff_output: str) -> dict[str, list[int]]:
    """Parse ``git diff -U0`` output to extract added line numbers per file.

    With ``-U0`` (zero context lines), each hunk header directly encodes
    which lines were added.  For a hunk ``@@ -a,b +c,d @@``, lines
    ``c`` through ``c+d-1`` are additions in the new file.

    Returns a mapping of ``file_path -> sorted deduplicated list of added
    line numbers``.
    """
    result: dict[str, list[int]] = {}
    current_file: str | None = None

    for raw in diff_output.splitlines():
        # +++ b/path/to/file.py
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            if current_file not in result:
                result[current_file] = []
            continue

        if current_file is None:
            continue

        m = _HUNK_RE.match(raw)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            # count == 0 means a pure deletion hunk (no lines added on the
            # new side), which we skip.
            if count > 0:
                result[current_file].extend(range(start, start + count))

    # Deduplicate and sort (hunks are already ordered, but be safe).
    for file_path in result:
        result[file_path] = sorted(set(result[file_path]))

    return result


def get_changed_lines(
    repo: Path,
    revision_or_range: str,
) -> dict[str, list[int]]:
    """Run ``git diff -U0`` and parse the result to get changed lines per file.

    Returns an empty dict on failure (bad revision, not a git repo, etc.)
    so callers degrade gracefully to file-level-only comments.
    """
    proc = subprocess.run(
        ["git", "diff", "-U0", revision_or_range],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    return parse_unified_diff(proc.stdout)


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------

_FOOTER = "\n\n---\n🏛️ *[Bruriah](https://github.com/leonardoprimero/bruriah) · Architectural Decision Intelligence*"


def _format_file_warning(w: DriftWarning) -> str:
    """Format a ``DriftWarning`` as a markdown PR review comment body.

    Placed on the file (no ``line``), so every reviewer sees the stale
    governance notice in the "Files changed" view.
    """
    sha_short = w.decision_sha[:8] if w.decision_sha else "unknown"

    parts: list[str] = [
        "⚠️ **Architectural Drift Detected**\n",
        (
            f"This file is governed by decision **{w.governing_decision}** "
            f"(`{sha_short}`) which was **{w.lineage_state}**"
        ),
    ]

    if w.current_active_decision:
        active_sha = (w.current_active_sha or "")[:8]
        parts[-1] += f" by **{w.current_active_decision}** (`{active_sha}`)."
    else:
        parts[-1] += "."

    if w.generations > 1:
        parts.append(
            f"\n↳ This decision evolved through **{w.generations} generations** to reach the current active decision."
        )

    if w.action_recommendation:
        parts.append(f"\n> {w.action_recommendation}")

    parts.append(_FOOTER)
    return "\n".join(parts)


def _format_line_warning(resolution: CausalResolution) -> str:
    """Format a ``CausalResolution`` with lineage alerts as a line-level comment.

    Uses the ACTUAL fields of ``CausalResolution`` and ``LineageAlert`` from
    ``why.py``: ``governing_decision.subject``, ``governing_decision.commit_sha``,
    and the alert's ``relation``, ``successor_ref``, ``successor_subject``, etc.
    """
    parts: list[str] = ["⚠️ **Stale Governance on This Line**\n"]

    if resolution.governing_decision:
        dec = resolution.governing_decision
        sha_short = dec.commit_sha[:8] if dec.commit_sha else "unknown"
        parts.append(f"This line is governed by decision **{dec.subject}** (`{sha_short}`).")

    if resolution.lineage_alerts:
        alert = resolution.lineage_alerts[0]
        succ_name = alert.successor_subject or alert.successor_ref
        succ_sha = (alert.successor_commit or "")[:8]
        parts.append(f"\nThis decision was **{alert.relation}** by **{succ_name}** (`{succ_sha}`).")

        if alert.active_successor_subject and alert.depth > 1:
            active_sha = (alert.active_successor_commit or "")[:8]
            parts.append(
                f"\n↳ Subsequently evolved through **{alert.depth} generations** "
                f"to **{alert.active_successor_subject}** (`{active_sha}`)."
            )

    parts.append(_FOOTER)
    return "\n".join(parts)


def _format_summary(
    report: DriftReport,
    stale_count: int,
    clean_count: int,
    unindexed_count: int,
) -> str:
    """Format the review summary body — the top-level comment on the PR."""
    total = len(report.inspected_files)

    parts: list[str] = [
        "## 🏛️ Bruriah Architectural Review\n",
        "| Metric | Count |",
        "|--------|------:|",
        f"| Files inspected | {total} |",
        f"| ⚠️ Drift warnings | {stale_count} |",
        f"| ✅ Clean governance | {clean_count} |",
        f"| ❓ Unindexed | {unindexed_count} |",
    ]

    if report.stale_warnings:
        parts.append("\n### Drift Warnings\n")
        for w in report.stale_warnings:
            sha_short = w.decision_sha[:8] if w.decision_sha else "?"
            line = f"- `{w.file_path}`: **{w.governing_decision}** (`{sha_short}`)"
            if w.current_active_decision:
                active_sha = (w.current_active_sha or "")[:8]
                line += f" → {w.lineage_state.lower()} by **{w.current_active_decision}** (`{active_sha}`)"
            parts.append(line)

        parts.append(
            "\n> **Action required**: Review each warning and either align with "
            "the active decision or add an `Amends:` trailer to document the "
            "architectural evolution."
        )
    else:
        parts.append("\n✅ All changes conform to active architectural decisions.")

    parts.append(_FOOTER)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Review builder
# ---------------------------------------------------------------------------


def build_review(
    report: DriftReport,
    repo: Path,
    database: sqlite3.Connection | None = None,
    changed_lines: dict[str, list[int]] | None = None,
    *,
    strict: bool = False,
    line_comments: bool = True,
) -> ReviewResult:
    """Build a complete PR review from a drift report.

    Parameters
    ----------
    report:
        The drift analysis report from ``analyze_architectural_drift``.
    repo:
        Path to the git repository root.
    database:
        Open SQLite connection for line-level causal archaeology.
        When ``None``, only file-level comments are generated.
    changed_lines:
        Mapping of ``file_path -> [line_numbers]`` for line-level comments.
        When ``None`` and ``database`` is provided, no line comments are
        generated (the caller should supply both or neither).
    strict:
        When ``True``, the review event is ``REQUEST_CHANGES`` if drift
        is detected; otherwise always ``COMMENT``.
    line_comments:
        When ``False``, skip line-level causal archaeology even if
        ``database`` and ``changed_lines`` are available.
    """
    comments: list[ReviewComment] = []
    stale_count = len(report.stale_warnings)
    clean_count = len(report.clean_files)
    unindexed_count = len(report.unindexed_files)

    # -- File-level comments for each drift warning --
    stale_files: set[str] = set()
    for w in report.stale_warnings:
        stale_files.add(w.file_path)
        comments.append(ReviewComment(path=w.file_path, body=_format_file_warning(w)))

    # -- Line-level comments for changed lines in stale files --
    if line_comments and database is not None and changed_lines is not None:
        for file_path in stale_files:
            lines = changed_lines.get(file_path, [])
            # Cap at 5 lines per file to avoid comment spam.
            sampled = lines[:5] if len(lines) > 5 else lines
            for line_num in sampled:
                try:
                    resolution = trace_causal_archaeology(repo, database, f"{file_path}:{line_num}")
                except WhyError:
                    continue
                if resolution.lineage_alerts:
                    comments.append(
                        ReviewComment(
                            path=file_path,
                            body=_format_line_warning(resolution),
                            line=line_num,
                            side="RIGHT",
                        )
                    )

    event = "REQUEST_CHANGES" if strict and report.has_drift else "COMMENT"
    summary = _format_summary(report, stale_count, clean_count, unindexed_count)

    return ReviewResult(
        body=summary,
        comments=tuple(comments),
        event=event,
        has_drift=report.has_drift,
        inspected_count=len(report.inspected_files),
        stale_count=stale_count,
        clean_count=clean_count,
        unindexed_count=unindexed_count,
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def format_review_json(review: ReviewResult) -> str:
    """Serialize a ``ReviewResult`` to JSON for programmatic consumption."""
    return json.dumps(asdict(review), indent=2, sort_keys=True)


def format_review_human(review: ReviewResult) -> str:
    """Format a ``ReviewResult`` for terminal display."""
    lines: list[str] = [review.body]

    if review.comments:
        lines.append("")
        lines.append(f"{'─' * 60}")
        lines.append(f"  INLINE COMMENTS ({len(review.comments)})")
        lines.append(f"{'─' * 60}")

        for c in review.comments:
            loc = f"{c.path}:{c.line}" if c.line else f"{c.path} (file-level)"
            lines.append(f"\n  ── {loc} ──")
            lines.append(c.body)

    return "\n".join(lines)
