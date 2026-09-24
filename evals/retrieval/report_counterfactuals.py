#!/usr/bin/env python3
"""Whether the counterfactual engine actually learned something new from GitHub, not just whether
retrieval improved.

    python evals/retrieval/report_counterfactuals.py --corpus leakcanary --data-dir /tmp/data-leakcanary \
        --questions evals/project-memory/leakcanary-issues.jsonl --out /tmp/lc-counterfactuals.jsonl

WHAT THIS MEASURES. `report_reach.py` (its sibling) answers "does the correct DOCUMENT rank
better with GitHub documents in the corpus". This script answers a narrower, harder question: for
each evaluation question that names the GitHub issue behind its answer (`provenance.issue` in the
`.jsonl` question files), does the index hold a REJECTED ALTERNATIVE or PREMISE traceable back to
that exact issue -- the counterfactual engine's own vocabulary (`investigate_work`'s "what was
tried and rejected"), not merely a passage that happens to rank higher. And in total: how many
rejected alternatives and premises does the engine now know, and how many of those came from
GitHub-derived documents (`bruriah corpus --github`) rather than from commit trailers (the
pre-existing `bruriah corpus` path).

TRACEABLE, PRECISELY. An `alternatives`/`premises` row (`bruriah.repository.AlternativeRow` /
`PremiseRow`) carries a `document_ref`, never an issue number -- the front-matter key
`corpus._metadata` reads it from (`issue: N`, written by `github_corpus._render_document`) is not
one of `SourceMetadata`'s fields (`bruriah/models.py`) and is therefore never persisted into the
index's stored `documents.metadata` column. The one signal the index DOES expose for a document is
its `relative_path` (`SnapshotRepository.scan_passages`), and `github_corpus.build_documents`
writes every GitHub-derived document into the SAME flat corpus directory the git corpus uses, so
`relative_path` is just the filename. That filename is therefore read literally, exactly the
convention `github_corpus.build_documents` documents and `test_github_corpus.py` pins:
`YYYY-MM-DD-issue-N-<slug>.md` (or `unknown-date-issue-N-<slug>.md` when the issue has neither a
`closed_at` nor a `created_at`). A row is "traceable to issue N" when its `document_ref` names a
document whose filename matches that pattern with that N. This reads the filename convention, not
front matter the index cannot see: a document whose `issue:` key was hand-edited after the fact
without renaming the file would not be found by this script, and neither would any code downstream
of `SourceMetadata` -- see `github_corpus.py`'s own module docstring on why the filename IS the
provenance carrier ("provenance without a contract change").

Reads the index only through `bruriah.repository.SnapshotRepository` -- `scan_passages`,
`get_alternatives`, `get_premises` -- never raw SQL, so this script keeps working across schema
changes the same way `report_reach.py` does for its own (private-helper) dependency on retrieval
internals; unlike that script, nothing here is private, because the repository already exposes
everything this measurement needs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.index_runner import EmbedderFactory, _default_embedder_factory  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.repository import AlternativeRow, PremiseRow, SnapshotRepository  # noqa: E402

_NO_DEADLINE = float("inf")

# `github_corpus.build_documents` writes `f"{date10}-issue-{number}-{_slug(title)}.md"`, with
# `date10` either an ISO date's first 10 characters or the fixed string `"unknown-date"` when the
# issue has neither `closed_at` nor `created_at` (see that module's `build_documents`). This is
# that exact convention, read back from a document's `relative_path`.
_GITHUB_DOCUMENT_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|unknown-date)-issue-(\d+)-.+\.md$")


def _never_expires() -> float:
    return 0.0


@dataclass(frozen=True)
class CounterfactualCoverage:
    """One corpus's counterfactual coverage, computed once over the whole index."""

    questions_total: int
    questions_with_issue: int
    questions_with_issue_document: int
    questions_with_alternative: int
    questions_with_premise: int
    alternatives_total: int
    alternatives_github: int
    alternatives_commit: int
    premises_total: int
    premises_github: int
    premises_commit: int


def github_issue_documents(repo: SnapshotRepository) -> dict[int, set[str]]:
    """Map GitHub issue number -> the `document_ref`s of documents whose filename names it.

    Reads every document once via `scan_passages`, the same public, non-SQL method
    `report_reach.py` reads passages through, rather than a document-listing method the
    repository does not expose. A corpus with zero passages returns an empty map, not an error."""
    passages, _stopped = repo.scan_passages(_NO_DEADLINE, _never_expires)
    issue_documents: dict[int, set[str]] = {}
    seen_documents: set[str] = set()
    for passage in passages:
        if passage.document_ref in seen_documents:
            continue
        seen_documents.add(passage.document_ref)
        filename = passage.relative_path.rsplit("/", 1)[-1]
        match = _GITHUB_DOCUMENT_RE.match(filename)
        if match:
            issue_documents.setdefault(int(match.group(1)), set()).add(passage.document_ref)
    return issue_documents


def counterfactual_coverage(
    repo: SnapshotRepository,
    questions: list[dict[str, Any]],
) -> tuple[CounterfactualCoverage, list[dict[str, Any]]]:
    """The aggregate report, plus one row per question that carries `provenance.issue`."""
    issue_documents = github_issue_documents(repo)
    github_document_refs = {ref for refs in issue_documents.values() for ref in refs}

    alternatives: list[AlternativeRow] = repo.get_alternatives()
    premises: dict[str, PremiseRow] = repo.get_premises()

    alternatives_github = sum(1 for alt in alternatives if alt.document_ref in github_document_refs)
    premises_github = sum(1 for premise in premises.values() if premise.document_ref in github_document_refs)

    rows: list[dict[str, Any]] = []
    for question in questions:
        provenance = question.get("provenance")
        issue = provenance.get("issue") if isinstance(provenance, dict) else None
        if issue is None:
            continue
        issue = int(issue)
        docs_for_issue = issue_documents.get(issue, set())
        rows.append(
            {
                "id": question["id"],
                "issue": issue,
                "has_issue_document": bool(docs_for_issue),
                "alternatives_traceable": any(alt.document_ref in docs_for_issue for alt in alternatives),
                "premises_traceable": any(premise.document_ref in docs_for_issue for premise in premises.values()),
            }
        )

    result = CounterfactualCoverage(
        questions_total=len(questions),
        questions_with_issue=len(rows),
        questions_with_issue_document=sum(1 for row in rows if row["has_issue_document"]),
        questions_with_alternative=sum(1 for row in rows if row["alternatives_traceable"]),
        questions_with_premise=sum(1 for row in rows if row["premises_traceable"]),
        alternatives_total=len(alternatives),
        alternatives_github=alternatives_github,
        alternatives_commit=len(alternatives) - alternatives_github,
        premises_total=len(premises),
        premises_github=premises_github,
        premises_commit=len(premises) - premises_github,
    )
    return result, rows


def render(corpus: str, result: CounterfactualCoverage) -> str:
    lines = [
        f"## {corpus}",
        "",
        f"{result.questions_with_issue} / {result.questions_total} questions carry "
        f"`provenance.issue`. Of those, **{result.questions_with_issue_document}** have a GitHub "
        f"document in the index, **{result.questions_with_alternative}** hold a traceable "
        f"rejected alternative, and **{result.questions_with_premise}** hold a traceable premise.",
        "",
        "| | total | GitHub-derived | commit-derived |",
        "|---|---|---|---|",
        f"| alternatives | {result.alternatives_total} | {result.alternatives_github} | {result.alternatives_commit} |",
        f"| premises | {result.premises_total} | {result.premises_github} | {result.premises_commit} |",
    ]
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    """`embedder_factory` exists for in-process testing (a fake embedder, the same way
    `test_cli.py` drives `cli.build_serve_deps`) and defaults to the real one for CLI use; it
    never changes which model is used -- `build_serve_deps` always reads that from the snapshot's
    own build descriptor, never from this factory's default argument."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="per-question traceability, as JSONL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
    deps = build_serve_deps(
        resolve_paths(cli_data_dir=args.data_dir, env={}),
        embedder_factory=embedder_factory,
    )
    try:
        repo = SnapshotRepository(deps.snapshot.database)
        result, rows = counterfactual_coverage(repo, questions)
    finally:
        deps.snapshot.database.close()

    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(asdict(result), indent=2, sort_keys=True) if args.json else render(args.corpus, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
