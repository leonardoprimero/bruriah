#!/usr/bin/env python3
"""Deprecated entry point. Use `bruriah corpus` instead.

The logic moved into the package as `bruriah.gitcorpus`, because this file was step ONE of the
documented workflow and no wheel ships `scripts/`: anyone who installed Bruriah rather than cloning
it was told, on the front page, to run a file they did not have.

    bruriah corpus --repo . --out ./corpus

This wrapper stays so the command in older notes and shell histories keeps working. It calls the
same code the subcommand does -- there is no second implementation to drift.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bruriah.gitcorpus import build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git_corpus", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."), help="repository to read")
    parser.add_argument("--out", type=Path, required=True, help="directory to write documents into")
    parser.add_argument("--limit", type=int, default=None, help="most recent N commits only")
    args = parser.parse_args(argv)

    print("note: `scripts/git_corpus.py` is deprecated; use `bruriah corpus`.", file=sys.stderr)
    if not (args.repo / ".git").exists():
        print(f"error: {args.repo} is not a git repository", file=sys.stderr)
        return 1
    # `build` returns what it read as well as what it wrote. This wrapper reports only the
    # numerator it always reported: it exists to keep an old command working, and the coverage
    # line belongs with the command people are being sent to.
    result = build(args.repo, args.out, args.limit)
    print(f"{result.written} decisions written to {args.out}")
    if result.written == 0:
        print(
            "No commit carried an explanatory body. This history records what changed but not why, "
            "so there is no reasoning to retrieve -- retrieval quality cannot compensate for that.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
