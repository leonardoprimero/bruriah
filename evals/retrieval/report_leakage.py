#!/usr/bin/env python3
"""Report ground-truth leakage for a question set, so the figures in the eval README are
reproducible by anyone holding the repository.

    bruriah corpus --repo . --out /tmp/corpus
    python evals/retrieval/report_leakage.py --corpus /tmp/corpus \
        --questions evals/project-memory/decisions-en.jsonl

This needs NO index and NO embedding model: leakage is a property of the questions and the corpus
text, not of retrieval. That is deliberate -- a check you can run before downloading a model is a
check people actually run, and it belongs BEFORE the expensive part of an eval rather than after.

Ground-truth filenames are recorded without the generator's sha8 (a sha does not survive a history
rewrite and the eval is meant to outlive one), so corpus filenames are stripped to match. Without
that, everything reports as unmatched and this looks like a corpus problem rather than a naming one
-- the same trap `evals/project-memory/README.md` records for the scorer.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from leakage import TermStatistics, leakage  # noqa: E402

_SHA = re.compile(r"^(\d{4}-\d{2}-\d{2})-[0-9a-f]{8}-(.*)$")


def canonical(name: str) -> str:
    """Strip the sha8 `bruriah corpus` inserts, so a corpus file matches recorded ground truth."""
    match = _SHA.match(name)
    return f"{match.group(1)}-{match.group(2)}" if match else name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="directory of derived .md documents")
    parser.add_argument("--questions", type=Path, required=True, help="a decisions-*.jsonl question set")
    parser.add_argument("--gate", type=float, default=None,
                        help="exit non-zero if any question's peak leakage is at or above this")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    documents = {canonical(path.name): path.read_text(encoding="utf-8")
                 for path in sorted(args.corpus.glob("*.md"))}
    if not documents:
        print(f"error: no .md documents under {args.corpus}", file=sys.stderr)
        return 2
    statistics = TermStatistics.over(documents.values())

    rows = []
    unmatched = []
    for line in args.questions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        wanted = case["ground_truth"]["must_include"]
        if not wanted or wanted[0] not in documents:
            unmatched.append(case["id"])
            continue
        result = leakage(case["query"], documents[wanted[0]], statistics)
        rows.append({
            "id": case["id"], "query": case["query"],
            "share": result.share, "peak": result.peak, "leaked": list(result.leaked[:5]),
        })

    scoreable = [row for row in rows if row["peak"] is not None]
    summary = {
        "corpus_documents": len(documents),
        "questions": len(rows),
        "unmatched_ground_truth": unmatched,
        "mean_share": (sum(r["share"] for r in scoreable) / len(scoreable)) if scoreable else None,
        "mean_peak": (sum(r["peak"] for r in scoreable) / len(scoreable)) if scoreable else None,
        "peak_at_or_above_half": sum(1 for r in scoreable if r["peak"] >= 0.5),
    }

    if args.json:
        print(json.dumps({"summary": summary, "questions": rows}, indent=2, sort_keys=True))
    else:
        print(f"corpus {summary['corpus_documents']} documents · {summary['questions']} questions")
        if unmatched:
            print(f"  ground truth not present in this corpus: {', '.join(unmatched)}")
        print(f"\n  {'id':6} {'share':>6} {'peak':>6}  leaked terms (most distinctive first)")
        for row in rows:
            share = "  --  " if row["share"] is None else f"{row['share']:6.2f}"
            peak = "  --  " if row["peak"] is None else f"{row['peak']:6.2f}"
            print(f"  {row['id']:6} {share} {peak}  {', '.join(row['leaked']) or '-'}")
        mean_share = "n/a" if summary["mean_share"] is None else f"{summary['mean_share']:.3f}"
        mean_peak = "n/a" if summary["mean_peak"] is None else f"{summary['mean_peak']:.3f}"
        print(f"\n  mean share {mean_share} · mean peak {mean_peak} "
              f"· {summary['peak_at_or_above_half']} question(s) with peak >= 0.50")

    if args.gate is not None:
        over = [r["id"] for r in scoreable if r["peak"] >= args.gate]
        if over:
            print(f"\nFAIL: peak leakage >= {args.gate} for {', '.join(over)}", file=sys.stderr)
            return 1
        print(f"\nOK: no question reaches peak leakage {args.gate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
