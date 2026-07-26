#!/usr/bin/env python3
"""Turn a repository's decision record into a corpus Bruriah can index.

This lives in the package, not in scripts/, because it is step ONE of the documented
workflow: a reader who installs the wheel has no scripts/ directory, and for a while the
front page opened by telling them to run a file they did not have.

A project's reasoning already exists: it is in the commit messages that explain WHY, written by the
person deciding at the moment of deciding. This reads that history and writes one Markdown document
per decision, so `bruriah index` can take it from there.

    bruriah corpus --repo . --out ./corpus
    bruriah index --corpus-root ./corpus --policy ./policy.yaml

Commits with no explanatory body are skipped: a subject line records what changed, never why, and
including them adds noise without reasoning. Merge commits are skipped for the same reason.

Nothing is written to the repository being read. This script only reads.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# git's own escapes, NOT literal control bytes: a process argument cannot contain a NUL, so
# building the format string with real \x00 raises ValueError before git is ever invoked. git
# expands these itself, and the output is then split on the real bytes.
_SEPARATOR_FMT, _RECORD_FMT = "%x00", "%x01"
_SEPARATOR, _RECORD = "\x00", "\x01"
_MAX_BODY = 16_000


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
    except FileNotFoundError:
        raise SystemExit("error: git is not on PATH")
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"error: git failed: {error.stderr.strip() or error}")
    return result.stdout


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:60].strip("-") or "untitled"


def build(repo: Path, out: Path, limit: int | None = None) -> int:
    """Write one document per commit that carries reasoning. Returns how many were written."""
    fmt = _SEPARATOR_FMT.join(["%H", "%aI", "%an", "%s", "%b"]) + _RECORD_FMT
    args = ["log", "--no-merges", f"--format={fmt}"]
    if limit:
        args.append(f"-{limit}")
    out.mkdir(parents=True, exist_ok=True)

    written = 0
    for entry in _git(repo, *args).split(_RECORD):
        parts = entry.strip("\n").split(_SEPARATOR)
        if len(parts) < 5:
            continue
        sha, when, author, subject, body = (part.strip() for part in parts[:5])
        if not body:
            continue  # a subject records what changed, never why
        files = _git(repo, "show", "--stat", "--format=", "--name-only", sha).split()

        document = (
            f"# {subject}\n\n"
            f"**Decided:** {when[:10]} · **Commit:** `{sha[:12]}` · **Author:** {author}\n\n"
            f"{body[:_MAX_BODY]}\n\n"
            "## Files this decision touched\n"
            + "".join(f"- `{item}`\n" for item in sorted(set(files))[:12])
        )
        # LF explicitly: these documents are hashed byte-for-byte by `parse_document`
        # (`source_hash`), so letting text mode rewrite the separators would make an index built
        # from a Windows-generated corpus disagree with one built from the same commits anywhere
        # else -- same history, different digests, for no reason a reader could ever see.
        (out / f"{when[:10]}-{sha[:8]}-{_slug(subject)}.md").write_text(
            document, encoding="utf-8", newline="\n"
        )
        written += 1
    return written
