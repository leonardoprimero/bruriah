#!/usr/bin/env python3
"""Turn a repository's decision record into a corpus Cerebro can index.

A project's reasoning already exists: it is in the commit messages that explain WHY, written by the
person deciding at the moment of deciding. This reads that history and writes one Markdown document
per decision, so `cerebro-mcp index` can take it from there.

    python scripts/git_corpus.py --repo . --out ./cerebro-corpus
    cerebro-mcp index --corpus-root ./cerebro-corpus --policy ./policy.yaml

Commits with no explanatory body are skipped: a subject line records what changed, never why, and
including them adds noise without reasoning. Merge commits are skipped for the same reason.

Nothing is written to the repository being read. This script only reads.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_SEPARATOR = "\x00"
_RECORD = "\x01"
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
    fmt = _SEPARATOR.join(["%H", "%aI", "%an", "%s", "%b"]) + _RECORD
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
        (out / f"{when[:10]}-{sha[:8]}-{_slug(subject)}.md").write_text(
            document, encoding="utf-8"
        )
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git_corpus", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."), help="repository to read")
    parser.add_argument("--out", type=Path, required=True, help="directory to write documents into")
    parser.add_argument("--limit", type=int, default=None, help="most recent N commits only")
    args = parser.parse_args(argv)

    if not (args.repo / ".git").exists():
        print(f"error: {args.repo} is not a git repository", file=sys.stderr)
        return 1
    written = build(args.repo, args.out, args.limit)
    print(f"{written} decisions written to {args.out}")
    if written == 0:
        print(
            "No commit carried an explanatory body. This history records what changed but not why, "
            "so there is no reasoning to retrieve -- retrieval quality cannot compensate for that.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
