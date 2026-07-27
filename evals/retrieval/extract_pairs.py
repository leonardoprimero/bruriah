#!/usr/bin/env python3
"""Extract candidate eval pairs whose question came from someone who did not know the answer.

    bruriah corpus --repo ./leakcanary --out /tmp/lc-corpus
    python evals/retrieval/extract_pairs.py \
        --repo ./leakcanary --github square/leakcanary --corpus /tmp/lc-corpus \
        --out evals/project-memory/leakcanary-candidates.jsonl

The decision logic lives in `pairs.py` and is pure; this file is the git and GitHub edge. It writes
TWO files: the surviving candidates, and every rejection with its reason. Publishing only the
keepers would be asking the reader to trust what was dropped, which is the habit this whole
measurement exists to replace.

What comes out is CANDIDATES. All four conditions are mechanical, including the fourth -- shape
rules over titles alone that exclude raw stack traces, release notes, titles under 18 characters,
and complaints naming no subject. That last one was first drafted as a human pass and it was wrong:
the ground truth is valid whether or not the title reads nicely (the commit closed that issue), and
curating further would mean choosing which questions are fair AFTER seeing what retrieval can do.
See `pairs.py` for the full argument.

API responses are cached to `--cache` so a second run costs nothing and so the exact inputs behind
a published number stay on disk, reviewable, instead of living only in whatever GitHub returned
that afternoon.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from pairs import Commit, Issue, build, corpus_index, parse_closes  # noqa: E402

_SEPARATOR_FMT, _RECORD_FMT = "%x00", "%x01"
_SEPARATOR, _RECORD = "\x00", "\x01"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"error: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _gh(*args: str) -> str | None:
    """`None` on any failure. A missing issue is data (it is reported as unresolvable), not a crash
    that loses the nine hundred lookups already done."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def read_commits(repo: Path) -> list[Commit]:
    """Every non-merge commit, in the same shape `gitcorpus.build` reads them."""
    fmt = _SEPARATOR_FMT.join(["%H", "%an", "%aI", "%s", "%b"]) + _RECORD_FMT
    commits: list[Commit] = []
    for entry in _git(repo, "log", "--no-merges", f"--format={fmt}").split(_RECORD):
        parts = entry.strip("\n").split(_SEPARATOR)
        if len(parts) < 5:
            continue
        sha, author, when, subject, body = (part.strip() for part in parts[:5])
        commits.append(Commit(
            sha=sha, author_name=author, author_login=None,
            authored_at=datetime.fromisoformat(when), subject=subject, body=body,
        ))
    return commits


class Cache:
    """A JSON file of everything fetched, so a rerun is free and the inputs stay auditable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = json.loads(path.read_text()) if path.is_file() else {}

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="a clone of the repository")
    parser.add_argument("--github", required=True, metavar="OWNER/NAME")
    parser.add_argument("--corpus", type=Path, required=True, help="output of `bruriah corpus`")
    parser.add_argument("--out", type=Path, required=True, help="candidate pairs, JSONL")
    parser.add_argument("--rejected", type=Path, default=None,
                        help="rejections with reasons (default: alongside --out)")
    parser.add_argument("--cache", type=Path, default=None,
                        help="API cache (default: alongside --out)")
    args = parser.parse_args(argv)

    rejected_path = args.rejected or args.out.with_suffix(".rejected.jsonl")
    cache = Cache(args.cache or args.out.with_suffix(".cache.json"))

    corpus = corpus_index(path.name for path in args.corpus.glob("*.md"))
    if not corpus:
        print(f"error: no corpus documents under {args.corpus}", file=sys.stderr)
        return 2

    commits = read_commits(args.repo)
    candidates = [commit for commit in commits if parse_closes(commit.body)]
    print(f"{len(commits)} non-merge commits · {len(candidates)} name an issue they close "
          f"· {len(corpus)} corpus documents", file=sys.stderr)

    # Only candidates need their GitHub login resolved, so the expensive lookup is bounded by how
    # many commits actually reference something rather than by the size of the history.
    resolved: list[Commit] = []
    for index, commit in enumerate(candidates, start=1):
        key = f"commit:{commit.sha}"
        entry = cache.get(key)
        if entry is None:
            raw = _gh("api", f"repos/{args.github}/commits/{commit.sha}", "--jq", "{login: .author.login}")
            entry = json.loads(raw) if raw else {"login": None}
            cache.put(key, entry)
        resolved.append(Commit(
            sha=commit.sha, author_name=commit.author_name, author_login=entry.get("login"),
            authored_at=commit.authored_at, subject=commit.subject, body=commit.body,
        ))
        if index % 25 == 0:
            print(f"  resolved {index}/{len(candidates)} commit authors", file=sys.stderr)
            cache.save()

    numbers = sorted({n for commit in resolved for n in parse_closes(commit.body)})
    issues: dict[int, Issue] = {}
    for index, number in enumerate(numbers, start=1):
        key = f"issue:{number}"
        entry = cache.get(key)
        if entry is None:
            raw = _gh("api", f"repos/{args.github}/issues/{number}", "--jq",
                      "{title: .title, login: .user.login, created: .created_at, pr: (.pull_request != null)}")
            entry = json.loads(raw) if raw else None
            cache.put(key, entry)
        if entry:
            issues[number] = Issue(
                number=number, title=entry["title"], author_login=entry["login"],
                created_at=datetime.fromisoformat(entry["created"].replace("Z", "+00:00")),
                is_pull_request=bool(entry["pr"]),
            )
        if index % 25 == 0:
            print(f"  resolved {index}/{len(numbers)} issues", file=sys.stderr)
            cache.save()
    cache.save()

    kept, rejections = build(resolved, issues, corpus)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for pair in kept:
            handle.write(json.dumps(asdict(pair), sort_keys=True) + "\n")
    with rejected_path.open("w", encoding="utf-8") as handle:
        for rejection in rejections:
            handle.write(json.dumps(asdict(rejection), sort_keys=True) + "\n")

    tally: dict[str, int] = {}
    for rejection in rejections:
        tally[rejection.reason] = tally.get(rejection.reason, 0) + 1
    print(f"\nkept {len(kept)} candidate pairs -> {args.out}", file=sys.stderr)
    print(f"rejected {len(rejections)} -> {rejected_path}", file=sys.stderr)
    for reason, count in sorted(tally.items(), key=lambda item: -item[1]):
        print(f"  {count:5}  {reason}", file=sys.stderr)
    print("\nThese are CANDIDATES: conditions 1-3. Condition 4 is a shape filter applied at "
          "scoring time, and what it excludes is published alongside what it keeps.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
